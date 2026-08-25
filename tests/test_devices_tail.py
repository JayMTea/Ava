"""devices._tail_jsonl must read the END of the log, not the whole file.

The device event log grows to MAX_BYTES (8MB by default) before rotation, and
_tail_jsonl runs per device on EVERY /api/devices refresh. The old reader
parsed every row of the file to hand back the last handful — ~119ms per device
per request at the rotation ceiling. The rewrite seeks to the tail and reads a
bounded window (64KB, doubling only while it holds fewer than `limit` parsable
rows, capped at the file size).

These tests pin the rewrite to the OLD reader's observable behavior — same
rows, same order, same edge cases — by comparing against a verbatim copy of
the old implementation, then cover the failure shapes an append-only log
actually produces: a torn trailing write, blank lines, corrupt rows, a single
row wider than the whole first window.
"""
import json

import pytest

from ava_bridge.devices import _tail_jsonl

CHUNK = 64 * 1024      # the rewrite's first window (keep in sync with devices.py)


def _naive_tail(path, limit):
    """The previous implementation, verbatim — the behavioral reference."""
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        return []
    return rows[-limit:] if limit else rows


def _rows(n, pad=0):
    return [{"ts": 1000.0 + i, "cid": "d", "name": f"e{i}", "note": "x" * pad}
            for i in range(n)]


def _jsonl(rows):
    return b"".join(json.dumps(r).encode() + b"\n" for r in rows)


# --------------------------------------------------------------------------- #
# Exact equivalence with the old reader on small files (< one chunk)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("limit", [1, 5, 0, 999])
def test_small_file_matches_the_naive_reader(tmp_path, limit):
    p = tmp_path / "log.jsonl"
    p.write_bytes(_jsonl(_rows(7)))
    assert _tail_jsonl(str(p), limit) == _naive_tail(str(p), limit)


def test_small_file_returns_the_actual_tail(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_bytes(_jsonl(_rows(7)))
    out = _tail_jsonl(str(p), 3)
    assert [r["name"] for r in out] == ["e4", "e5", "e6"]   # file order kept


@pytest.mark.parametrize("limit", [1, 5, 0])
def test_no_trailing_newline_still_includes_the_last_row(tmp_path, limit):
    p = tmp_path / "log.jsonl"
    p.write_bytes(_jsonl(_rows(4))[:-1])          # complete row, no final \n
    out = _tail_jsonl(str(p), limit)
    assert out == _naive_tail(str(p), limit)
    assert out[-1]["name"] == "e3"


@pytest.mark.parametrize("limit", [1, 5, 0])
def test_blank_lines_are_skipped(tmp_path, limit):
    rows = _rows(5)
    body = b"\n\n".join(json.dumps(r).encode() for r in rows) + b"\n   \n\n"
    p = tmp_path / "log.jsonl"
    p.write_bytes(body)
    assert _tail_jsonl(str(p), limit) == _naive_tail(str(p), limit)


@pytest.mark.parametrize("limit", [1, 3, 0])
def test_a_corrupt_middle_line_is_skipped(tmp_path, limit):
    rows = _rows(6)
    body = _jsonl(rows[:3]) + b"{this is not json}\n" + _jsonl(rows[3:])
    p = tmp_path / "log.jsonl"
    p.write_bytes(body)
    assert _tail_jsonl(str(p), limit) == _naive_tail(str(p), limit)


def test_a_torn_trailing_write_is_tolerated(tmp_path):
    """Mid-append, the last line is half a record — skip it, keep the rest."""
    p = tmp_path / "log.jsonl"
    p.write_bytes(_jsonl(_rows(5)) + b'{"ts": 1099.0, "cid": "d", "na')
    out = _tail_jsonl(str(p), 2)
    assert out == _naive_tail(str(p), 2)
    assert [r["name"] for r in out] == ["e3", "e4"]


def test_empty_file(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_bytes(b"")
    assert _tail_jsonl(str(p), 5) == []
    assert _tail_jsonl(str(p), 0) == []


def test_missing_file(tmp_path):
    p = tmp_path / "absent.jsonl"
    assert _tail_jsonl(str(p), 5) == []
    assert _tail_jsonl(str(p), 0) == []


# --------------------------------------------------------------------------- #
# Files larger than one window — the case the rewrite exists for
# --------------------------------------------------------------------------- #
def test_big_file_returns_the_right_tail(tmp_path):
    rows = _rows(3000, pad=80)                    # ~140B/row → well past 64KB
    p = tmp_path / "log.jsonl"
    p.write_bytes(_jsonl(rows))
    assert p.stat().st_size > CHUNK
    out = _tail_jsonl(str(p), 5)
    assert out == _naive_tail(str(p), 5)
    assert [r["name"] for r in out] == [f"e{i}" for i in range(2995, 3000)]


def test_limit_wider_than_one_window_forces_the_widening_path(tmp_path):
    rows = _rows(3000, pad=80)                    # ~468 rows fit in 64KB
    p = tmp_path / "log.jsonl"
    p.write_bytes(_jsonl(rows))
    for limit in (1000, 0):                       # both must widen to the answer
        assert _tail_jsonl(str(p), limit) == _naive_tail(str(p), limit)


def test_a_single_row_wider_than_the_window_is_still_found(tmp_path):
    """One row larger than 64KB: the first window holds only its tail, which
    must be discarded as partial and then recovered by doubling."""
    big = {"ts": 1.0, "cid": "d", "name": "big", "blob": "y" * (CHUNK + 4096)}
    rows = [big] + _rows(2)
    p = tmp_path / "log.jsonl"
    p.write_bytes(_jsonl(rows))
    for limit in (3, 0):
        out = _tail_jsonl(str(p), limit)
        assert out == _naive_tail(str(p), limit)
        assert out[0]["name"] == "big"


def test_big_file_tail_after_a_torn_trailing_write(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_bytes(_jsonl(_rows(1500, pad=80)) + b'{"ts": 9')
    out = _tail_jsonl(str(p), 4)
    assert out == _naive_tail(str(p), 4)
    assert [r["name"] for r in out] == [f"e{i}" for i in range(1496, 1500)]
