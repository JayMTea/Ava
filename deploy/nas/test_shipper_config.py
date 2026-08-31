"""deploy/nas/shipper.yaml through the shipper's own loader, then through its engine on fakes.

Needs the `datastack.shipper` package (home-lab/data-stack). Skipped in Ava's own suite; run it
from the data-stack checkout:

    uv run pytest -c pyproject.toml /path/to/ava-suite/Ava/deploy/nas/test_shipper_config.py

No bucket, no warehouse, no network: the streams run against FakeLanding / FakeWarehouse over a
temp copy of the state tree, with records shaped exactly like the live ledgers.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

shipper_config = pytest.importorskip("datastack.shipper.config")
shipper_testing = pytest.importorskip("datastack.shipper.testing")
import yaml  # noqa: E402

CONFIG = Path(__file__).with_name("shipper.yaml")

# The warehouse pack's raw tables (home-lab/data-stack/connectors/ava/manifest.yaml). Every
# ENABLED stream with a target must land in one of them.
PACK_TABLES = {
    "raw_ava.llm_generation", "raw_ava.turn_event", "raw_ava.hw_sample", "raw_ava.hw_sample_1h",
    "raw_ava.device_event", "raw_ava.alloc_decision", "raw_ava.kpi_daily",
    "raw_ava.kpi_definition",
}


def _data(src: Path, state: Path) -> dict[str, Any]:
    """The committed config with /src and /state re-pointed at temp dirs."""
    text = CONFIG.read_text(encoding="utf-8")
    text = text.replace("path: /src/", f"path: {src.as_posix()}/")
    text = text.replace("state_dir: /state", f"state_dir: {state.as_posix()}")
    return yaml.safe_load(text)


@pytest.fixture
def cfg(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    return shipper_config.parse_config(_data(src, tmp_path / "state")), src


def test_the_committed_file_loads_as_is() -> None:
    cfg = shipper_config.load_config(CONFIG)
    assert cfg.app == "ava" and cfg.bucket == "ava-bronze"
    assert cfg.source_name == "ava-bridge", "must equal the warehouse pack's manifest name"
    assert cfg.clickhouse.user == "ava_ingestor"
    assert cfg.clickhouse.password_env == "DATASTACK_CH_AVA_INGESTOR_PASSWORD"
    assert cfg.s3.endpoint == "https://platform-minio-1:9000"
    assert cfg.s3.access_key_env == "MINIO_AVA_ACCESS_KEY"
    assert cfg.s3.secret_key_env == "MINIO_AVA_SECRET"
    assert cfg.state_dir == Path("/state")


def test_every_enabled_target_is_a_pack_table_and_every_path_is_under_src() -> None:
    cfg = shipper_config.load_config(CONFIG)
    for s in cfg.streams:
        if s.kind != "pg_poll":
            assert str(s.path).startswith("/src/"), s.name
        if s.enabled and s.target is not None:
            assert s.target.table in PACK_TABLES, f"{s.name} -> {s.target.table} has no raw table"
    assert {s.target.table for s in cfg.enabled_streams if s.target} == PACK_TABLES


def test_chat_messages_are_declared_and_off() -> None:
    """The privacy decision must stay visible, not become a missing line."""
    cfg = shipper_config.load_config(CONFIG)
    s = cfg.stream("chat_message")
    assert not s.enabled and s.kind == "sqlite_poll"
    assert s.target is not None and s.target.table == "raw_ava.chat_message"
    assert "chat_message" not in {x.name for x in cfg.enabled_streams}


def test_uploaded_media_is_declared_off_bronze_only_and_content_addressed() -> None:
    """`media_uploads` replaces the `mc mirror` the README used to carry.

    Off because turning it on is the operator's call: `ava-bronze` is versioned with no Delete,
    so a first pass over an unsized tree cannot be undone with the `ava-app` key.
    """
    cfg = shipper_config.load_config(CONFIG)
    s = cfg.stream("media_uploads")
    assert not s.enabled and s.kind == "file_tree"
    # Bronze only: a JPEG is not rows, and config.py refuses a target on this kind outright.
    assert s.target is None and s.bronze_prefix == "media"
    assert s.source["path"] == "/src/media/uploads"
    assert s.source["layout"] == "content", "the sha in the key is the checksum"
    assert "**/*.part" in s.source["exclude"], "a half-written upload is a permanent version"
    assert s.source["max_files_per_tick"] >= 1, "one thread: a first pass must not stall the tails"
    assert "media_uploads" not in {x.name for x in cfg.enabled_streams}


def test_the_audit_stream_is_chain_verified_and_snapshots_are_nightly() -> None:
    cfg = shipper_config.load_config(CONFIG)
    assert cfg.stream("audit").audit_chain
    assert cfg.stream("audit").target.table == "raw_ava.turn_event"
    for name in ("chats_db", "memory_db"):
        s = cfg.stream(name)
        assert s.kind == "sqlite_snapshot" and s.interval_seconds == 86400 and s.target is None
    assert cfg.nightly_at == "02:15"


def test_the_compose_fragment_mounts_this_config_and_declares_the_networks() -> None:
    fragment = yaml.safe_load(Path(__file__).with_name("shipper.compose.yml").read_text())
    svc = fragment["services"]["ava-shipper"]
    assert svc["image"] == "datastack-shipper:0.1.0"
    assert svc["user"] == "1000:10"
    assert "/volume1/apps/ava:/src:ro" in svc["volumes"]
    assert "/volume1/apps/ava-shipper:/state" in svc["volumes"]
    assert "./nas/shipper.yaml:/etc/shipper/shipper.yaml:ro" in svc["volumes"]
    assert set(svc["networks"]) == {"datastack_warehouse", "minio_s3"}
    assert fragment["networks"]["datastack_warehouse"]["external"] is True
    assert fragment["networks"]["minio_s3"]["external"] is True
    assert svc["deploy"]["resources"]["limits"]["memory"] == "256m"
    assert "env_file" not in svc, "Ava's .env must not be handed to the sidecar wholesale"
    # Credentials by NAME: every env value is an interpolation, never a literal secret.
    for key, value in svc["environment"].items():
        assert re.match(r"^\$\{[A-Z_]+(:[-?].*)?\}$", str(value)), (key, value)


# ── the streams, end to end on the fakes ──────────────────────────────────────

PERF_LINE = {
    "ts": 1787956883.196, "iso": "2026-08-28T22:41:23", "host": "fbbc5ad0e1d9",
    "category": "llm", "serving": "openai", "endpoint": "chat/completions",
    "served_by": "backend", "served_label": "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4",
    "served_model": "nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4",
    "served_url": "http://100.120.254.9:8002/v1", "status": 200, "prompt_tokens": 28,
    "completion_tokens": 104, "total_tokens": 132, "ttft_ms": 2559.2, "gen_seconds": 0.001,
    "total_seconds": 2.56, "stream_chunks": 1, "params": {"max_tokens": 800, "stream": False},
}
HW_LINE = {"gpu_util": 0.0, "gpu_temp": 52.87, "gpu_power": None, "mem_used_pct": 12.33,
           "mem_used_gb": 7.77, "cpu": 3.86, "ts": 1787942810.6}
ALLOC_LINE = {
    "ts": 1787950000.1, "event": "admit", "model": "vllm-omni", "lease_id": "abc", "enforcing": False,
    "admit": True, "gated": False, "free_gib": 40.5, "need_gib": 12.0, "raw_free_gib": 41.0,
    "reserved_gib": 0.5, "projected_gib": 28.5, "shortfall_gib": 0.0,
    "steps": [{"model": "x", "mode": "keep", "expect_gib": 1.0, "speculative": False, "reason": ""}],
    "released": [], "reason": "fits", "note": "",
}
KPI_ROW = {"day": "2026-08-28", "metric": "steps", "dim": "watch", "value": 8123, "unit": "count",
           "state": "ok", "provenance": "pull", "n": 1, "lo": None, "hi": None, "def": "1a2b3c4d",
           "src": "healthapp", "observed_at": 1787950000.0}
DEF_ROW = {"def": "1a2b3c4d", "metric": "steps", "recorded_at": 1787940000.0, "unit": "count",
           "agg": "sum", "source": "healthapp", "declares": {"unit": "count"}, "definition": "steps/day"}
DEVICE_LINE = {"ts": 1787950001.0, "cid": "healthapp", "type": "reading", "name": "hr", "value": 61.0,
               "unit": "bpm"}


def _audit_lines() -> list[str]:
    """Three chained records, the rule from ava_bridge/audit.py: prev = sha256(previous line)."""
    import hashlib

    lines: list[str] = []
    prev = ""
    for seq, kind in enumerate(("turn", "egress", "approval"), start=1):
        evt = {"ts": 1787950000.0 + seq, "seq": seq, "prev": prev, "kind": kind, "actor": "owner",
               "chat_id": "c1", "status": "done", "tools": ["weather"], "connector": "healthapp"}
        line = json.dumps(evt, ensure_ascii=False)
        lines.append(line)
        prev = hashlib.sha256(line.encode("utf-8")).hexdigest()
    return lines


def _write(path: Path, *records: dict[str, Any], raw_lines: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as f:
        for r in records:
            f.write((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8"))
        for line in raw_lines or []:
            f.write((line + "\n").encode("utf-8"))


def test_every_ledger_stream_ships_and_inserts_typed_rows(cfg) -> None:
    config, src = cfg
    _write(src / "logs/performance.jsonl", PERF_LINE)
    _write(src / "logs/audit.jsonl", raw_lines=_audit_lines())
    _write(src / "logs/kpi/ledger.jsonl", KPI_ROW)
    _write(src / "logs/kpi/definitions.jsonl", DEF_ROW)
    _write(src / "logs/alloc.jsonl", ALLOC_LINE)
    _write(src / "logs/hw_history/hw_1m.jsonl", HW_LINE)
    _write(src / "logs/hw_history/hw_1h.jsonl", HW_LINE)
    _write(src / "logs/devices/healthapp.jsonl", DEVICE_LINE)

    landing = shipper_testing.FakeLanding(bucket="ava-bronze")
    warehouse = shipper_testing.FakeWarehouse()
    engine = shipper_testing.make_engine(config, landing, warehouse)

    for s in config.enabled_streams:
        if s.kind == "jsonl_tail":
            engine.run_stream(s)
            assert not engine.store.load(s.name).last_error, (s.name, engine.store.load(s.name).last_error)

    by_table = {t: (cols, rows) for t, cols, rows, _ in warehouse.inserts}
    assert set(by_table) == PACK_TABLES

    cols, rows = by_table["raw_ava.llm_generation"]
    row = dict(zip(cols, rows[0], strict=True))
    assert row["observed_at"].isoformat().startswith("2026-08-28T22:41:23")
    assert row["status"] == 200 and row["ttft_ms"] == 2559.2
    assert json.loads(row["params"]) == {"max_tokens": 800, "stream": False}
    assert isinstance(row["_source_run_id"], uuid.UUID) and row["_source_name"] == "ava-bridge"

    cols, rows = by_table["raw_ava.turn_event"]
    seqs = [dict(zip(cols, r, strict=True))["seq"] for r in rows]
    assert seqs == [1, 2, 3]
    first = dict(zip(cols, rows[0], strict=True))
    assert first["prev"] == "" and json.loads(first["tools"]) == ["weather"]

    cols, rows = by_table["raw_ava.alloc_decision"]
    row = dict(zip(cols, rows[0], strict=True))
    assert row["enforcing"] == 0 and row["admit"] == 1 and json.loads(row["steps"])[0]["model"] == "x"

    cols, rows = by_table["raw_ava.hw_sample"]
    row = dict(zip(cols, rows[0], strict=True))
    assert row["gpu_power"] is None and row["cpu"] == 3.86

    cols, rows = by_table["raw_ava.kpi_daily"]
    row = dict(zip(cols, rows[0], strict=True))
    assert row["def_hash"] == "1a2b3c4d" and row["value"] == 8123.0 and row["lo"] is None

    cols, rows = by_table["raw_ava.kpi_definition"]
    row = dict(zip(cols, rows[0], strict=True))
    assert json.loads(row["declares"]) == {"unit": "count"}

    cols, rows = by_table["raw_ava.device_event"]
    row = dict(zip(cols, rows[0], strict=True))
    assert row["cid"] == "healthapp" and row["value"] == "61.0"

    # Bronze: the exact bytes, under the stream's prefix, as x-ndjson.
    keys = sorted(landing.objects)
    assert any(k.startswith("perf/dt=") and k.endswith(".jsonl") for k in keys)
    audit_key = next(k for k in keys if k.startswith("audit/dt="))
    assert landing.objects[audit_key][0] == ("\n".join(_audit_lines()) + "\n").encode("utf-8")
    assert landing.objects[audit_key][1] == "application/x-ndjson"

    # The chain objects were recorded for the nightly verification.
    assert engine.store.load("audit").data["chain_objects"]


def test_a_second_tick_ships_nothing_new(cfg) -> None:
    config, src = cfg
    _write(src / "logs/performance.jsonl", PERF_LINE)
    landing = shipper_testing.FakeLanding(bucket="ava-bronze")
    warehouse = shipper_testing.FakeWarehouse()
    engine = shipper_testing.make_engine(config, landing, warehouse)
    perf = config.stream("perf")
    assert engine.run_stream(perf) == (1, 1)
    assert engine.run_stream(perf) == (0, 0)
    assert landing.puts == 1 and len(warehouse.inserts) == 1


def test_missing_ledgers_are_nothing_to_ship_not_failures(cfg) -> None:
    """A connector with no device stream yet, an allocator that never ran: no file, no error."""
    config, _src = cfg
    engine = shipper_testing.make_engine(
        config, shipper_testing.FakeLanding(bucket="ava-bronze"), shipper_testing.FakeWarehouse()
    )
    for name in ("devices_arqaid", "alloc", "kpi_ledger"):
        assert engine.run_stream(config.stream(name)) == (0, 0)
        assert not engine.store.load(name).last_error
