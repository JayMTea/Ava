"""Connector registry — Ava's pluggable integration layer.

A *connector* is a small manifest (`connector.yaml`) describing one thing Ava
monitors or drives: its health probe, its performance-log source, its egress
policy, and its agent actions. The dashboard service matrix, the performance
aggregator, and the left-rail app nav read this registry directly (drop in a
folder → they update); the agent's tools + egress policy are generated from the
same manifest by `ava connector tools|policies --write` (then `agent/install.sh`
deploys them). Either way: adding an app is a manifest, not core-code edits.

Manifests are discovered from (later overrides earlier):
  1. built-in   <repo>/connectors/<id>/connector.yaml    (shipped first-party)
  2. user       $AVA_HOME/connectors/<id>/connector.yaml  (user-added)

Manifest paths may use ${AVA_HOME} ${AVA_LOGS} ${AVA_DATA} ${ROOT} plus any
exported process env var (e.g. an app's own ${MYAPP_ROOT}). See docs/CONNECTOR_SDK.md.
"""
import os
import shutil as _shutil
import time
from typing import Dict, List

from . import config, settings

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None

BUILTIN_DIR = os.path.join(config.ROOT, "connectors")
USER_DIR = settings.home("connectors")

_VARS = {
    "AVA_HOME": str(settings.AVA_HOME),
    "AVA_LOGS": config.LOGS_DIR,
    "AVA_DATA": config.DATA_HOME,
    "ROOT": config.ROOT,
}

_cache: Dict[str, object] = {"ts": 0.0, "list": None}


import copy as _copy
import json as _json
import re as _re

_ENV_VAR = _re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(val):
    """Expand ${VAR} references: first the built-in connector vars (AVA_HOME,
    AVA_LOGS, AVA_DATA, ROOT), then any remaining ${NAME} from the process
    environment. ${NAME:-default} falls back to `default` when NAME is unset
    or empty — so one manifest can serve bare metal and Docker.
    """
    if not isinstance(val, str):
        return val
    for k, v in _VARS.items():
        val = val.replace("${%s}" % k, v or "")
    val = _ENV_VAR.sub(
        lambda m: settings.env_secret(m.group(1)) or (m.group(2) or ""), val)
    return val


def _static_actions(m: dict) -> list:
    """The statically-declared actions for a connector, tolerating both shapes:
    the list form ``actions: [ {...} ]`` and the dict form
    ``actions: { static: [...], discover: {...} }``.
    """
    acts = m.get("actions")
    if isinstance(acts, list):
        return [a for a in acts if isinstance(a, dict)]
    if isinstance(acts, dict):
        return [a for a in (acts.get("static") or []) if isinstance(a, dict)]
    return []


def _discover_spec(m: dict) -> dict | None:
    """A connector's dynamic tool-discovery spec, or None. Manifest form:
        actions:
          discover: { base: "${APP_URL}", list: "/tools", call: "/call",
                      token_env: APP_TOKEN }
    ``base`` defaults to the connector's base_url(); list/call default to
    /tools and /call.
    """
    acts = m.get("actions")
    if isinstance(acts, dict) and isinstance(acts.get("discover"), dict):
        d = acts["discover"]
        return {"base": _expand(d.get("base")) or None,
                "list": d.get("list") or "/tools",
                "call": d.get("call") or "/call",
                "token_env": d.get("token_env")}
    return None


# Manifests that failed to parse on the last real load — surfaced so a bad
# connector.yaml doesn't just vanish silently (Hub warning + `ava doctor`).
_load_errors: List[dict] = []


# The one id shape every management surface can address: the Hub routes gate on
# the same pattern (`_ID_RE` in hub/connectors.py) and `scaffold.valid_id`
# enforces it at creation time. A folder outside it still LOADS — hiding the
# app would be worse — it just cannot be managed from Setup, and the loader
# says so instead of leaving the owner to discover it route by route.
_FOLDER_ID_RE = _re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def _load_dir(base: str, errors: list | None = None) -> dict:
    out: dict = {}
    if yaml is None or not os.path.isdir(base):
        return out

    def _err(row: dict) -> None:
        if errors is not None:
            errors.append(row)

    for name in sorted(os.listdir(base)):
        if name.startswith(("_", ".")):
            continue
        path = os.path.join(base, name, "connector.yaml")
        if not os.path.isfile(path):
            continue
        try:
            try:
                with open(path, encoding="utf-8") as f:
                    m = yaml.safe_load(f) or {}
            except Exception as e:  # noqa: BLE001 — a bad manifest must not crash boot
                _err({"id": name, "path": path, "error": str(e)})
                continue
            if not isinstance(m, dict):  # e.g. a YAML list/scalar — not a manifest
                _err({"id": name, "path": path,
                      "error": "not a mapping (expected 'key: value' lines)"})
                continue
            # THE FOLDER NAME IS THE ID. The registry used to key on the
            # manifest's `id:` while every Hub lifecycle route keyed on the
            # folder — a mismatch made an app undeletable under the id the UI
            # displayed, and deletable under its folder name with none of the
            # grant/credential/tier cleanup that delete performs. One identity,
            # decided here, ends the split brain. A declared `id:` that agrees
            # is fine; one that disagrees is reported and ignored. And because
            # the folder name is always a str, a manifest like `id: 2048` can
            # no longer poison `out[m["id"]]` for every other connector.
            declared = m.get("id")
            m["id"] = name
            if isinstance(declared, bool):
                _err({"id": name, "path": path, "severity": "warn",
                      "error": f"`id:` parsed as YAML boolean ({declared}) — "
                               "quote it if you meant text; using the folder "
                               f"name '{name}'"})
            elif declared is not None and str(declared) != name:
                _err({"id": name, "path": path, "severity": "error",
                      "error": f"`id: {declared}` does not match its folder "
                               f"'{name}' — the folder name is the id; rename "
                               "one to match (see docs/CONNECTOR_SDK.md)"})
            if not _FOLDER_ID_RE.match(name):
                _err({"id": name, "path": path, "severity": "warn",
                      "error": f"folder name '{name}' cannot be managed from "
                               "Setup (ids must match [a-z][a-z0-9_-]{1,31}) — "
                               "rename the folder to a lowercase slug"})
            _validate(m, path, errors)
            m["_dir"] = os.path.join(base, name)
            out[name] = m
        except Exception as e:  # noqa: BLE001 — one bad manifest may only lose itself
            _err({"id": name, "path": path,
                  "error": f"failed to load: {type(e).__name__}: {e}"})
            continue
    return out


# The manifest schema Ava's core actually reads, and the type each block must be.
# Anything absent is fine; anything present with the WRONG type is quarantined.
# Derived from what the core actually reads off a manifest, not from what looks
# tidy — `dynamic_access`, `base_url` and `role` are all live and were missing
# from a first, guessed version of this table, which then warned about the
# repo's own shipped connectors.
_BLOCK_TYPES: dict = {
    "service": dict, "perf": dict, "ui": dict, "egress": dict, "auth": dict,
    "mcp": dict, "chat_pickup": dict, "jobs": dict, "discover": dict,
    "facade": dict, "health": (dict, str), "ingest": dict, "tools": (list, dict),
    "dynamic_access": (dict, bool), "actions": (list, dict), "model_hints": list,
    # What of this machine's compute belongs to the app (see `owns()`).
    "owns": dict,
    "id": str, "label": str, "kind": str, "role": str, "base_url": str,
    "token_env": str, "call": str,
    # `confirm` accepts BOTH shapes because `_author_confirm` implements both
    # (`confirm: true` for the whole connector, `confirm: [names]` for specific
    # actions). Declaring it `bool` here meant `_validate` popped the list form
    # before it ever reached the gate, so an author who wrote
    # `confirm: [publish_app]` got no confirmation and an error row they had to go
    # looking for. It survived because tests/test_approvals.py exercises the list
    # form through a mocked `load`, so `_validate` never ran on that path.
    "enabled": bool, "confirm": (bool, list), "manifest_version": int,
    # Opt in to letting a REMOTE server's self-reported `access:` tiers set
    # consent. Private/loopback connectors are trusted without it; see
    # `_trusts_declared_tiers`.
    "trust_declared_tiers": bool,
}

# What this Ava understands. A manifest declaring a NEWER version still loads —
# forward-compat by ignoring unknown keys is what keeps a fork's manifests
# portable — it just says so.
MANIFEST_VERSION = 1


def _declared_urls(m: dict):
    """[(label, raw, expanded)] for the URL-shaped values core dials."""
    out = []

    def add(label, raw):
        if raw:
            out.append((label, str(raw), _expand(str(raw))))

    svc = m.get("service")
    if isinstance(svc, dict):
        add("service.probe", svc.get("probe"))
    acts = m.get("actions")
    disc = acts.get("discover") if isinstance(acts, dict) else None
    if isinstance(disc, dict):
        add("actions.discover.base", disc.get("base"))
    mcp = m.get("mcp")
    if isinstance(mcp, dict):
        add("mcp.url", mcp.get("url"))
    ui = m.get("ui")
    if isinstance(ui, dict):
        add("ui.url", ui.get("url"))
    return out


def _bad_url(raw: str, url: str) -> str | None:
    """Why core cannot dial this URL, or None.

    `ava device new` writes `base: "http://127.0.0.1:PORT"` for the owner to
    edit, and an unedited one used to validate CLEAN: the connector loaded, a
    full egress policy rendered, and the only symptom was a connection error at
    discovery time that named neither the manifest nor the placeholder. Checking
    that the port parses catches the placeholder and a fat-fingered port alike,
    without this function having to know the template's vocabulary.

    An UNSET `${VAR}` is not that bug and must not be reported as one. Every
    example manifest that documents "export HASS_URL first" expands to nothing
    until the owner does, and the first version of this check turned all of them
    red on a clean checkout — an error message telling the owner their example
    is malformed when they have simply not configured it yet. So a value that
    interpolates and comes back without a scheme is left alone; once the var IS
    set, whatever it produced is checked like anything else.
    """
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError as e:
        return f"is not a URL ({e})"
    if parts.scheme not in ("http", "https"):
        if "${" in raw:
            return None
        return "needs an http:// or https:// scheme"
    try:
        _ = parts.port  # the parse IS the check
    except ValueError:
        tail = url.rsplit(":", 1)[-1].split("/")[0]
        hint = (" — that is the placeholder `ava device new` writes for you to "
                "replace") if tail == "PORT" else ""
        return f"has a non-numeric port {tail!r}{hint}"
    if not parts.hostname:
        return "has no host"
    return None


_NESTED_ERR = "`%s.%s:` must be %s (got %s) — ignoring that field; see docs/CONNECTOR_SDK.md"


def _validate_nested(m: dict, cid: str, path: str, errors: list) -> None:
    """Type-check the NESTED fields core actually dereferences.

    `_BLOCK_TYPES` stops at the top level, and every audit finding in this
    family had the same shape: a nested value of the wrong type sails through
    validation and detonates in whichever consumer dereferences it — `apps()`
    sorting on `ui.order`, `render_egress_policy` iterating `egress.hosts`, the
    Hub row builder walking `mcp.env`. Same quarantine contract as the
    top-level checks: report, drop the FIELD (never the connector), never touch
    the owner's file. Two shapes are coerced rather than dropped, because the
    author's intent is unambiguous: a quoted `ui.order: "10"` and a bare-string
    `egress.hosts: "127.0.0.1:9000"`.
    """

    def bad(block: str, key: str, want: str) -> None:
        got = type(m[block][key]).__name__
        errors.append({"id": cid, "path": path, "severity": "error",
                       "error": _NESTED_ERR % (block, key, want, got)})
        m[block].pop(key, None)

    ui = m.get("ui")
    if isinstance(ui, dict):
        if "order" in ui and not isinstance(ui["order"], bool):
            try:
                ui["order"] = int(ui["order"])
            except (TypeError, ValueError):
                bad("ui", "order", "a number")
        elif isinstance(ui.get("order"), bool):
            bad("ui", "order", "a number")
        if "api" in ui and not isinstance(ui["api"], dict):
            bad("ui", "api", "a mapping")

    eg = m.get("egress")
    if isinstance(eg, dict):
        for key in ("routes", "hosts"):
            v = eg.get(key)
            if v is None:
                continue
            if isinstance(v, str):
                eg[key] = [v]  # the un-bracketed single entry — intent is clear
            elif not isinstance(v, list):
                bad("egress", key, "a list of strings")
        if "host_rules" in eg and not isinstance(eg["host_rules"], dict):
            bad("egress", "host_rules", "a mapping")

    mcp = m.get("mcp")
    if isinstance(mcp, dict):
        if "env" in mcp and not isinstance(mcp["env"], dict):
            shape = ("the docker-compose LIST form — use the mapping form "
                     "`env: {NAME: value}`" if isinstance(mcp["env"], list)
                     else "a mapping")
            got = type(mcp["env"]).__name__
            errors.append({"id": cid, "path": path, "severity": "error",
                           "error": f"`mcp.env:` must be a mapping (got {got}"
                                    f"{' — looks like ' + shape if isinstance(mcp['env'], list) else ''})"
                                    " — ignoring that field; see docs/CONNECTOR_SDK.md"})
            mcp.pop("env", None)
        if "command" in mcp and not isinstance(mcp["command"], (list, str)):
            bad("mcp", "command", "a string or list")
        if "url" in mcp and not isinstance(mcp["url"], str):
            bad("mcp", "url", "a string")

    jobs = m.get("jobs")
    if isinstance(jobs, dict):
        if "labels" in jobs and not isinstance(jobs["labels"], dict):
            bad("jobs", "labels", "a mapping")

    acts = m.get("actions")
    if isinstance(acts, dict):
        if "static" in acts and not isinstance(acts["static"], list):
            bad("actions", "static", "a list")
        if "discover" in acts and not isinstance(acts["discover"], dict):
            bad("actions", "discover", "a mapping")


def _validate(m: dict, path: str, errors: list | None) -> None:
    """Type-check the blocks core reads, and QUARANTINE the bad ones in memory.

    Degrade per-block, never per-connector, and never per-page. A scalar where a
    mapping belonged used to sail through here and raise `AttributeError` deep in
    a consumer — and for `egress:` that consumer is the Setup -> Connectors page,
    which holds both the manifest editor and the error list. Breaking it is a
    self-inflicted lockout: the one screen that could fix the manifest is the one
    the manifest takes down.

    The deletion is on the IN-MEMORY copy only. The owner's file is never
    rewritten — they asked for that YAML, and a loader that "repairs" it would
    lose whatever they were in the middle of writing.
    """
    if errors is None:
        return
    cid = m.get("id", "?")
    declared = m.get("manifest_version")
    if isinstance(declared, int) and declared > MANIFEST_VERSION:
        errors.append({"id": cid, "path": path, "severity": "warn",
                       "error": f"written for manifest v{declared}; this Ava "
                                f"understands v{MANIFEST_VERSION} — unknown blocks "
                                "are ignored"})
    for key, want in _BLOCK_TYPES.items():
        if key not in m:
            continue
        if not isinstance(m[key], want):
            names = (want.__name__ if isinstance(want, type)
                     else " or ".join(t.__name__ for t in want))
            errors.append({"id": cid, "path": path, "severity": "error",
                           "error": f"`{key}:` must be {names} (got "
                                    f"{type(m[key]).__name__}) — ignoring that block; "
                                    "see docs/CONNECTOR_SDK.md"})
            m.pop(key, None)
    # Unknown top-level keys are how a typo (`egres:`) goes unnoticed forever.
    # A key that is not even TEXT (`on:` -> True, `1:` -> int) used to crash the
    # scan itself with AttributeError — one such key anywhere took down every
    # registry consumer. It is quarantined like any other bad block.
    for key in list(m):
        if not isinstance(key, str):
            errors.append({"id": cid, "path": path, "severity": "warn",
                           "error": f"key `{key!r}` is not text (YAML parsed it as "
                                    f"{type(key).__name__}) — ignored. Quote it if "
                                    "you meant a literal key."})
            m.pop(key, None)
            continue
        if key not in _BLOCK_TYPES and not key.startswith("x_"):
            errors.append({"id": cid, "path": path, "severity": "warn",
                           "error": f"unknown key `{key}:` — ignored. Prefix your own "
                                    "with `x_` to silence this."})
    _validate_nested(m, cid, path, errors)
    # A misspelled consent tier is a security defect, not a cosmetic one: the
    # author asked for a gate and the value decides which. `_dynamic_access` now
    # fails closed on one, but silence here is how it stayed unnoticed — the block
    # was type-checked and its VALUES never were.
    da = m.get("dynamic_access")
    if isinstance(da, dict):
        for pattern, tier in da.items():
            if str(tier or "").lower() not in _TIERS:
                errors.append({"id": cid, "path": path, "severity": "error",
                               "error": f"`dynamic_access: {{{pattern}: {tier}}}` is not "
                                        f"a consent tier ({', '.join(_TIERS)}) — that "
                                        "pattern now resolves to `destructive` (fail "
                                        "closed), so the tool asks every time until "
                                        "the spelling is fixed"})
    for a in _static_actions(m):
        acc = a.get("access")
        if acc is not None and str(acc).lower() not in _TIERS:
            errors.append({"id": cid, "path": path, "severity": "error",
                           "error": f"action `{a.get('id')}` declares `access: {acc}`, "
                                    f"not a consent tier ({', '.join(_TIERS)}) — it is "
                                    "being inferred from the HTTP method instead"})
    for label, raw, url in _declared_urls(m):
        why = _bad_url(raw, url)
        if why:
            # The RAW value only. `_expand` resolves `${VAR}` through the SECRET
            # STORE as well as the environment, so the expanded form can contain a
            # credential — and this string is rendered verbatim in the Hub's
            # malformed-manifest banner. Showing `${APP_TOKEN} -> hunter2` there
            # would put the secret on screen, in a page the owner might well be
            # screen-sharing while asking why their connector will not load. The
            # raw form is also the more useful half: it is what they have to edit.
            errors.append({"id": cid, "path": path, "severity": "error",
                           "error": f"`{label}: {raw}` — {why}"})


def _merge_all(errors: list | None = None) -> dict:
    merged: dict = {}
    merged.update(_load_dir(BUILTIN_DIR, errors))
    if os.path.realpath(USER_DIR) != os.path.realpath(BUILTIN_DIR):
        merged.update(_load_dir(USER_DIR, errors))  # user overrides built-in by id
    return merged


def load(force: bool = False) -> List[dict]:
    if not force and _cache["list"] is not None and time.time() - _cache["ts"] < 30:
        return _cache["list"]  # type: ignore[return-value]
    global _load_errors
    errors: list = []
    merged = _merge_all(errors)
    _load_errors = errors
    items = [m for m in merged.values() if m.get("enabled", True)]
    items.sort(key=lambda m: (0 if m.get("kind") == "core" else 1, m.get("id")))
    _cache.update(ts=time.time(), list=items)
    return items


def catalog(errors: list | None = None) -> List[dict]:
    """Every manifest INCLUDING disabled ones, for management UIs (so a disabled
    connector can still be seen and re-enabled). Not cached — management is rare.

    Validates. `_validate` only runs when an `errors` list is passed, and this
    used to pass none — so the ONE surface built to show a broken manifest, the
    connector management page, was the surface seeing manifests with their bad
    blocks still attached. A wrong-typed `actions:` was quarantined for the agent
    (which calls `load()`) and live for the editor, which is backwards: the editor
    is where you look at what is wrong.

    Pass a list to collect the errors; the default keeps the quarantine and
    discards the report, for callers that only want the manifests.
    """
    items = list(_merge_all(errors if errors is not None else []).values())
    items.sort(key=lambda m: (0 if m.get("kind") == "core" else 1, m.get("id")))
    return items


def load_errors() -> List[dict]:
    """Manifests that failed to parse on the last real load (id / path / error)."""
    return list(_load_errors)


def services() -> List[dict]:
    """Service-matrix entries for the dashboard (name / unit / probe / kind).
    `feature` optionally names the features.* flag that governs this service —
    when the user turned that feature off, a dead probe means "off by choice",
    not "down", and the dashboard must not paint it red."""
    out = []
    for m in load():
        s = m.get("service")
        if not s:
            continue
        out.append({
            "id": m["id"],
            "name": s.get("name", m.get("label", m["id"])),
            "unit": s.get("unit"),
            "probe": _expand(s.get("probe")),
            # Which claim this probe makes: `2xx` (default), `non5xx` (the old
            # behaviour, now opt-in and explicit), or an exact status code.
            "expect": s.get("expect"),
            "feature": s.get("feature"),
            "kind": m.get("kind", "app"),
        })
    return out


def perf_sources() -> Dict[str, str]:
    """app-key -> performance.jsonl path, for the performance aggregator."""
    out: Dict[str, str] = {}
    for m in load():
        p = m.get("perf") or {}
        path = _expand(p.get("path"))
        if path:
            out[p.get("app") or m["id"]] = path
    return out


def chat_pickups() -> List[dict]:
    """Resolved chat-artifact pickup specs for connectors declaring a
    ``chat_pickup:`` block — after a turn used one of the named tools, the
    bridge polls the app's log for artifacts produced during the turn and
    attaches them as chat quick-cards. Manifest form:

        chat_pickup:
          after_actions: [preview]     # this connector's generated tools
          after_tools: [my_raw_tool]   # optional extra raw tool names
          path: "/api/log"             # GET base_url + path
          params: { kind: preview, limit: 12 }
          list_key: log                # JSON key holding rows (newest-first)
          ts_key: ts                   # row field compared to turn start
          fields: { persona: persona, url: url, seed: seed, theme: theme }

    ``url_prefix`` is `/apps/<id>` for iframe apps so app-relative artifact
    URLs resolve through the same-origin app proxy (cookie-authed).
    """
    out: List[dict] = []
    for m in load():
        cp = m.get("chat_pickup")
        if not isinstance(cp, dict):
            continue
        cid = m["id"]
        tools = [f"{cid}_{a}" for a in (cp.get("after_actions") or [])]
        tools += [str(t) for t in (cp.get("after_tools") or [])]
        base = _expand(cp.get("base")) or base_url(cid)
        if not tools or not base or not cp.get("path"):
            continue
        ui = m.get("ui") or {}
        out.append({
            "id": cid,
            "tools": tools,
            "url": base.rstrip("/") + str(cp["path"]),
            "params": cp.get("params") or {},
            "list_key": cp.get("list_key") or "log",
            "ts_key": cp.get("ts_key") or "ts",
            "fields": cp.get("fields") or {},
            "url_prefix": f"/apps/{cid}" if ui.get("embed") == "iframe" else "",
        })
    return out


def job_sources() -> List[dict]:
    """Live-job polling specs for connectors declaring a ``jobs:`` block —
    lets the hardware bubble attribute GPU memory to named tasks. Manifest form:

        jobs:
          path: "/api/jobs"          # GET base_url + path
          params: { active: 1 }
          list_key: jobs             # rows carry kind/stage/progress
          engine: Whisper
          labels: { transcribe: "Transcription" }   # kind -> human label
    """
    out: List[dict] = []
    for m in load():
        jb = m.get("jobs")
        if not isinstance(jb, dict):
            continue
        base = _expand(jb.get("base")) or base_url(m["id"])
        if not base or not jb.get("path"):
            continue
        out.append({
            "id": m["id"],
            "url": base.rstrip("/") + str(jb["path"]),
            "params": jb.get("params") or {},
            "list_key": jb.get("list_key") or "jobs",
            "engine": jb.get("engine"),
            "labels": {str(k): str(v) for k, v in (jb.get("labels") or {}).items()},
        })
    return out


def model_hints() -> List[dict]:
    """Loaded-model attribution hints merged from all connectors:

        model_hints:
          - { match: [substr1, substr2], role: "Speech recognition" }

    The hardware monitor checks these when labelling what a loaded model is FOR.
    """
    out: List[dict] = []
    for m in load():
        for h in (m.get("model_hints") or []):
            if isinstance(h, dict) and h.get("match") and h.get("role"):
                out.append({"match": [str(s).lower() for s in (h["match"] or [])],
                            "role": str(h["role"])})
    return out


def owns() -> List[dict]:
    """Which running things each connector claims as its own:

        owns:
          container: [myapp-llm]                 # exact docker name(s)
          cmdline:   ["--served-model-name myapp", "/myapp/main.py"]
          paths:     ["/opt/myapp"]              # prefix of its weight files

    The hardware monitor uses this to tell a CONNECTED APP's engine apart from
    unrelated software on the box. Both used to render identically, so a
    third-party model looked like one of Ava's.

    Declared, not inferred. Matching a connector's `base_url` port against
    whatever a process happens to be listening on would be wrong more often
    than right: an app's manifest names the port ITS API answers on, while the
    engine it runs sits on a different one entirely.

    Same shape as `model_hints()` — lowercased substrings, matched by the
    caller — so an author who has written one already knows how this behaves.
    """
    out: List[dict] = []
    for m in load():
        o = m.get("owns")
        if not isinstance(o, dict):
            continue
        cid = str(m.get("id") or "").strip()
        if not cid:
            continue

        def _strs(key: str, blk=o) -> List[str]:
            v = blk.get(key)
            if isinstance(v, str):
                v = [v]
            return [str(s).strip().lower() for s in (v or []) if str(s).strip()]

        entry = {"id": cid, "container": _strs("container"),
                 "cmdline": _strs("cmdline"), "paths": _strs("paths")}
        if entry["container"] or entry["cmdline"] or entry["paths"]:
            out.append(entry)
    return out


def actions() -> List[dict]:
    """All agent actions declared by connectors (id + description + connector).

    Includes statically-declared actions and, for a connector whose tools are
    resolved at run time, a single synthetic ``<id>`` discovery bridge action.

    BOTH dynamic shapes count, not just the facade. This function predates
    ``mcp:`` support and went on testing ``_discover_spec`` alone, while every
    other reader of the same distinction -- transport(), render_egress_policy(),
    tool_files(), agent_surface() -- was taught the pair. So an MCP-only
    connector reported no actions at all, even though the generated policy
    granted it __tools/__call and the agent called its tools perfectly well.

    Where that showed: ``GET /api/apps/{cid}/actions``, whose ActionConsole is
    the ENTIRE tile for an ``ui.embed: none`` app. A working 25-tool MCP app
    rendered "This app declares no agent actions." -- the one shape of app for
    which the console is the only thing the owner ever sees.
    """
    out = []
    for m in load():
        for a in _static_actions(m):
            if a.get("id"):
                out.append({"connector": m["id"], "id": a["id"],
                            "description": a.get("description", "")})
        if _discover_spec(m) or _mcp_spec(m):
            out.append({"connector": m["id"], "id": m["id"],
                        "description": f"Dynamic tools discovered from the {m.get('label', m['id'])} app",
                        "dynamic": True})
    return out


# Where sandboxed agent tools reach the host bridge (OpenClaw's host alias).
# The sandbox-side name for the host. Owned by the runtime package: it is
# OpenShell's docker topology, not a fact about Ava.
from .runtime import nemoclaw_layout as _layout
from .runtime.nemoclaw_layout import BRIDGE_HOST as _BRIDGE_HOST
# Derived, not pinned. `server.port` / AVA_PORT is documented in
# config.example.yaml and deploy/README.md, and this literal is why using it broke
# every agent tool silently: the generated egress policy allowed 8096 and the
# generated tool dialled 8096, so a bridge moved to another port was blocked twice
# and nothing in the failure named a port.
def _bridge_port() -> int:
    """The port the generated egress policy must allow through to the bridge.

    Resolved PER CALL, not frozen at import. It used to be a module constant, so
    a `server.port` change picked up by `provision.port_rewrite` and by
    install.sh's own `sed` — both of which resolve it live — was NOT picked up
    here. The rendered policy then allowed one port while the rewrite expected
    another, and the failure is the worst kind this file produces: the sandbox
    blocks the call and nothing in the error names a port.
    """
    from . import config
    return int(config.SERVER_PORT)
_PRIVATE_IPS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
_TOOL_BINARIES = [{"path": "/usr/local/bin/node"}, {"path": "/usr/bin/node"},
                  {"path": "/usr/bin/curl"}]


def _is_private_host(host: str) -> bool:
    """True for a loopback/RFC1918/link-local literal or a `.local`/`.internal`
    name — i.e. a host for which the RFC1918 SSRF allow-list is the point rather
    than a hole. Anything unresolvable-as-private is treated as public, which is
    the safe direction: it means the allow-list is withheld."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    if h in ("localhost",) or h.endswith((".local", ".internal", ".localhost")):
        return True
    import ipaddress
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _connector_host(m: dict) -> str:
    """The host a connector's tools actually reach: its MCP url, else its discover
    base, else `base_url`, else `ui.url`. Empty when the manifest names none."""
    from urllib.parse import urlparse
    for url in (((m.get("mcp") or {}) if isinstance(m.get("mcp"), dict) else {}).get("url"),
                (((m.get("actions") or {}) if isinstance(m.get("actions"), dict) else {})
                 .get("discover") or {}).get("base"),
                m.get("base_url"),
                ((m.get("ui") or {}) if isinstance(m.get("ui"), dict) else {}).get("url")):
        if isinstance(url, str) and url.strip():
            return (urlparse(_expand(url)).hostname or "").lower()
    return ""


def _trusts_declared_tiers(m: dict) -> bool:
    """May this connector's own `access:` self-report set Ava's consent tier?

    Yes for a private/loopback app — the owner installed it, and the ava-tools/1
    facade's tiers are the author telling Ava about their own code. No for a remote
    server, whose tool list can change without the owner touching Ava, unless the
    owner opts in with `trust_declared_tiers: true`.
    """
    t = m.get("trust_declared_tiers")
    if isinstance(t, bool):
        return t
    host = _connector_host(m)
    return bool(host) and _is_private_host(host)


def _default_port(host: str) -> int:
    """A manifest `hosts: ["example.com"]` used to render `port: 80` via
    `int(port or 80)`, silently pointing a plaintext-HTTP policy at an
    HTTPS-only service. A bare public hostname means 443; a private one keeps 80,
    which is what a LAN app on a bare host actually serves."""
    return 80 if _is_private_host(host) else 443


def _dedupe_rules(rules: list) -> list:
    """Drop repeated allow-rules, keeping the first of each (method, path)."""
    seen, out = set(), []
    for r in rules:
        a = r.get("allow") or {}
        key = (str(a.get("method", "")).upper(), str(a.get("path", "")))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def policy_preset_name(cid: str) -> str:
    """The egress preset name the SANDBOX knows a connector's policy by.

    Namespaced under `ava-`, but never doubled for a connector whose id already
    starts with it — `ava-notes` stays `ava-notes`, not `ava-ava-notes`.

    A function rather than an inline expression because two places need the same
    answer and they are in different modules: this renders the policy, and
    `provision.connector_items` uses the name to find that policy's row in the
    drift report. When the second one re-derived the rule it got it wrong for
    exactly the ids this special case exists for, and the failure was silent —
    the row simply never matched, so a per-connector deploy asserted nothing
    about its own policy and reported success either way.
    """
    return cid if cid.startswith("ava-") else f"ava-{cid}"


def render_egress_policy(cid: str) -> dict | None:
    """Render a connector's `egress` block into an OpenClaw egress-policy dict
    (same shape as agent/policies/*.yaml). Returns None if it declares no egress.

    Manifest form:
        egress:
          routes: ["POST /internal/foo", "GET /internal/bar"]  # bridge routes
          hosts:  ["127.0.0.1:9000"]                            # direct endpoints
    Actions that declare a `path` are generic-proxy actions: they call
    ``/internal/connector/<id>/<action>`` on the bridge, so that route is
    allow-listed automatically.
    """
    m = {x["id"]: x for x in load()}.get(cid)
    if not m:
        return None
    eg = m.get("egress") or {}
    if not isinstance(eg, dict):
        eg = {}
    # Normalized here as well as in `_validate_nested`: this function is called
    # with manifests from `load()`, but `ava connector policies` can reach it
    # through paths that skipped validation, and a bare string iterated here
    # per-CHARACTER once rendered 84 blanket allow rules from one typo.
    _routes = eg.get("routes") or []
    if isinstance(_routes, str):
        _routes = [_routes]
    if not isinstance(_routes, list):
        _routes = []
    _hosts = eg.get("hosts") or []
    if isinstance(_hosts, str):
        _hosts = [_hosts]
    if not isinstance(_hosts, list):
        _hosts = []
    _host_rules = eg.get("host_rules")
    if not isinstance(_host_rules, dict):
        _host_rules = {}
    endpoints = []
    rules = []
    for r in _routes:
        parts = str(r).split()
        method, path = (parts[0], parts[1]) if len(parts) == 2 else ("GET", str(r))
        rules.append({"allow": {"method": method.upper(), "path": path}})
    # Auto-allow the generic proxy route for each generic-proxy action. Both
    # methods are allowed so GET-style tools work as well as POST ones (the route
    # is host-local + internal-token gated). Meta-tool connectors skip this: the
    # agent reaches them only through __tools/__call below.
    if not meta_static(m):
        for a in _static_actions(m):
            if a.get("id") and a.get("path"):
                path = f"/internal/connector/{cid}/{a['id']}"
                rules.append({"allow": {"method": "GET", "path": path}})
                rules.append({"allow": {"method": "POST", "path": path}})
    # Auto-allow the discovery bridge routes for a dynamic connector — the
    # HTTP list+call facade or a real MCP server (`mcp:` block) alike. For MCP
    # this IS the whole agent-side surface: the server itself stays outside
    # the sandbox; only these two policed routes are reachable. Large static
    # connectors use the same two routes via their find/call meta tools.
    if _discover_spec(m) or _mcp_spec(m) or meta_static(m):
        rules.append({"allow": {"method": "GET",
                                "path": f"/internal/connector/{cid}/__tools"}})
        rules.append({"allow": {"method": "POST",
                                "path": f"/internal/connector/{cid}/__call"}})
    # A manifest may also spell out a route this function derives — declaring
    # `GET /internal/connector/<id>/__tools` under egress.routes is the obvious
    # thing to write, and all three shipped demo manifests did. The rule then
    # appeared twice in the generated policy. Duplicates change nothing about
    # what is allowed, but a security policy that repeats itself is one a
    # reviewer has to diff line by line to be sure the copies agree. First
    # occurrence wins, so the manifest's own ordering survives.
    rules = _dedupe_rules(rules)
    if rules:
        endpoints.append({
            "host": _BRIDGE_HOST, "port": _bridge_port(), "protocol": "rest",
            "enforcement": "enforce", "allowed_ips": list(_PRIVATE_IPS),
            "rules": rules,
        })
    for h in _hosts:
        host, _, port = str(h).partition(":")
        ep = {"host": host, "port": int(port) if port else _default_port(host),
              "protocol": "rest", "enforcement": "enforce"}
        # NO `allowed_ips` here, deliberately. NemoClaw's policy-add permits that
        # key only on the bridge endpoint above (every hand-written policy in
        # agent/policies/ carries it there and nowhere else) and REJECTS the
        # whole preset when a user endpoint declares it — so the private-host
        # branch that used to add it rendered policies that could never apply:
        # `install.sh` reported "policy was NOT applied" and the connector's
        # tools hit deny-by-default. (Found wiring an owner app whose
        # manifest declares a loopback MCP sidecar under `egress.hosts`.) The endpoint's
        # own host:port is already the narrow grant; the sandbox guard's
        # private-space rules are nemoclaw's to enforce, not this renderer's to
        # pre-authorise.
        # An endpoint with no `rules` permits every method on every path, and
        # policy_inventory._scan only harvests wildcards FROM rules — so the
        # broadest grant a manifest can express was invisible to
        # ava_security_check.check_policy_wildcards. Make it explicit and visible.
        # `egress.host_rules` narrows it; the default says out loud what a bare
        # `hosts:` entry has always meant.
        hr = _host_rules.get(str(h)) or _host_rules.get(host)
        if hr:
            ep["rules"] = [{"allow": {"method": str(x).split()[0].upper(),
                                      "path": str(x).split()[1]}}
                           for x in hr if len(str(x).split()) == 2]
        else:
            # Explicit verbs, NOT `method: "*"`. No hand-written policy in
            # agent/policies/ uses a wildcard method, so its behaviour in OpenClaw
            # is unverified — and if the matcher compares the string exactly, `"*"`
            # would match no request at all, turning "allow everything" into "allow
            # nothing" and silently breaking every connector that declares
            # `egress.hosts`. Enumerating the verbs keeps the previous semantics
            # using only shapes that already ship, and `path: "/**"` still makes the
            # breadth visible to check_policy_wildcards, which was the point.
            ep["rules"] = [{"allow": {"method": v, "path": "/**"}}
                           for v in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")]
        endpoints.append(ep)
    if not endpoints:
        return None
    pname = policy_preset_name(cid)
    return {
        "preset": {"name": pname,
                   "description": f"Auto-generated egress for the {m.get('label', cid)} connector"},
        "network_policies": {pname: {"name": pname, "endpoints": endpoints,
                                     "binaries": list(_TOOL_BINARIES)}},
    }


def find_action(cid: str, aid: str) -> dict | None:
    m = {x["id"]: x for x in load()}.get(cid) or {}
    for a in _static_actions(m):
        if a.get("id") == aid:
            return a
    return None


def base_url(cid: str) -> str | None:
    """The connector's own host-local API base (for the generic action proxy).

    Resolution order: top-level `base_url` -> `ui.url` (an iframe app's own
    origin) -> the origin of `service.probe`.
    """
    from urllib.parse import urlparse
    m = {x["id"]: x for x in load()}.get(cid) or {}
    if m.get("base_url"):
        return _expand(m["base_url"]).rstrip("/")
    ui_url = _expand((m.get("ui") or {}).get("url") or "")
    if ui_url:
        return ui_url.rstrip("/")
    probe = _expand((m.get("service") or {}).get("probe") or "")
    if probe:
        u = urlparse(probe)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}"
    return None


def proxy_token_env(m: dict) -> str | None:
    """The env-var NAME whose value may be PRESENTED TO THE APP at ``ui.url``.

    Deliberately narrower than ``auth_env``, and the difference is a security
    boundary rather than a tidy-up. ``auth_env`` answers "which credential slot
    does this connector own?" — the question the Hub's credential field asks, and
    for an ``mcp:`` connector the honest answer is the MCP server's token. The app
    proxy asks a different question: "which credential is this app's own?" Those
    coincide for a REST app and diverge for an MCP one.

    They used to share one implementation, so a manifest carrying BOTH
    ``mcp.token_env`` and ``ui.embed: iframe`` had the MCP server's bearer
    attached to every proxied request to ``ui.url`` — handing a token issued by
    one host to a different host. The Setup Hub's own Connect-an-app form emits
    exactly that manifest (an MCP url + a token + the sidebar-tile option), and
    ``connectors/persona/connector.yaml`` carries a hand-written ``auth:`` block
    whose comment exists only to work around it.

    So: only a credential the manifest states belongs to the APP is proxyable —
    ``auth.token_env`` (the REST app's own token, which the SSO contract in
    docs/CONNECTOR_SDK.md §3 is about) or ``ui.api.token_env`` (declared for the
    browser data-proxy). An ``mcp``/``discover`` token is server-side only and is
    never presented to a UI.
    """
    if not isinstance(m, dict):
        return None
    auth = m.get("auth")
    if isinstance(auth, dict) and auth.get("token_env"):
        return str(auth["token_env"])
    ui = m.get("ui")
    if isinstance(ui, dict) and isinstance(ui.get("api"), dict) \
            and ui["api"].get("token_env"):
        return str(ui["api"]["token_env"])
    return None


def auth_env(m: dict) -> str | None:
    """The env-var NAME a connector uses for its app credential, across every
    manifest shape — REST ``auth``, an ``mcp`` server, a ``discover`` facade, or
    a browser data-proxy (``ui.api``). Returns the first declared, or ``None`` if
    the app needs no auth. The Setup Hub uses this to show whether a credential is
    saved and to store one keyed by name — the value itself never enters the
    manifest (see settings.env_secret / the Ava-never-has-passwords invariant).

    For what may be sent TO the app, use ``proxy_token_env`` — this function is
    deliberately broader and includes server-side-only tokens."""
    if not isinstance(m, dict):
        return None
    auth = m.get("auth")
    if isinstance(auth, dict) and auth.get("token_env"):
        return str(auth["token_env"])
    mcp = m.get("mcp")
    if isinstance(mcp, dict) and mcp.get("token_env"):
        return str(mcp["token_env"])
    acts = m.get("actions")
    if isinstance(acts, dict) and isinstance(acts.get("discover"), dict) \
            and acts["discover"].get("token_env"):
        return str(acts["discover"]["token_env"])
    ui = m.get("ui")
    if isinstance(ui, dict) and isinstance(ui.get("api"), dict) \
            and ui["api"].get("token_env"):
        return str(ui["api"]["token_env"])
    return None


TRANSPORTS = ("mcp", "discover", "rest", "none")


def transport(m: dict) -> str:
    """HOW a connector's tools reach Ava — the honest name for its wire protocol:

        mcp       a real Model Context Protocol server (``mcp:``), spoken by
                  ava_bridge/mcp_client.py
        discover  the ava-tools/1 HTTP facade (``actions.discover``) — MCP-shaped
                  but Ava's own protocol, NOT MCP
        rest      statically declared ``actions:`` proxied to the app's REST API
        none      no agent surface at all (a UI-only app, or a push-only device)

    Kept as one function because the distinction is easy to fudge and was: the
    Setup UI used to badge everything with tools as "MCP", which made the label
    meaningless on every row. Anything showing a transport to the owner reads it
    from here."""
    if not isinstance(m, dict):
        return "none"
    if _mcp_spec(m):
        return "mcp"
    if _discover_spec(m):
        return "discover"
    if _static_actions(m):
        return "rest"
    return "none"


def _auth_headers(cid: str) -> dict:
    """Bearer header for a connector's own API, from a top-level
    ``auth: { token_env: ENV }`` block. Empty if none declared."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    tenv = (m.get("auth") or {}).get("token_env")
    tok = settings.env_secret(tenv) if tenv else None
    return {"Authorization": "Bearer " + tok} if tok else {}


def call_action(cid: str, aid: str, args: dict | None) -> tuple:
    """Forward a generic-proxy action to the connector's own API (host-local).

    Returns (data, status). Used by the bridge route
    ``/internal/connector/<cid>/<aid>`` that the generated tools call. Supports
    real REST shapes: ``{tmpl}`` path params filled from args (and consumed),
    GET args sent as query params (else JSON body), the action's ``method``, an
    optional per-action ``base`` override, and the connector's bearer ``auth``.
    """
    a = find_action(cid, aid)
    if not a:
        return {"error": f"unknown action {cid}/{aid}"}, 404
    if not a.get("path"):
        return {"error": f"action {cid}/{aid} is not a generic-proxy action"}, 400
    base = _expand(a.get("base")) or base_url(cid)
    if not base:
        return {"error": f"connector {cid} has no base_url/probe to proxy to"}, 400
    args = dict(args or {})
    path = str(a.get("path"))
    for key in _re.findall(r"\{(\w+)\}", path):   # fill {tmpl} path params
        if key in args:
            path = path.replace("{%s}" % key, str(args.pop(key)))
    url = base.rstrip("/") + path
    method = str(a.get("method") or "POST").upper()
    headers = {"Content-Type": "application/json", **_auth_headers(cid)}
    import requests
    try:
        # allow_redirects=False: this is the AGENT's path to a connector's API, and
        # `headers` may carry the app's bearer. A followed redirect would let the
        # app steer a token-bearing request to a host or path the manifest never
        # declared — bypassing the egress policy, which constrains what the SANDBOX
        # may reach, not what the bridge fetches on its behalf.
        if method == "GET":
            r = requests.request("GET", url, params=args, headers=headers, timeout=60,
                                 allow_redirects=False)
        else:
            r = requests.request(method, url, json=args, headers=headers, timeout=200,
                                 allow_redirects=False)
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {"text": r.text[:4000]}
        return data, r.status_code
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}, 502


def render_tool(cid: str, action: dict) -> str:
    """Generate the .mjs source for a connector action's agent tool.

    The tool calls the bridge's generic proxy, which forwards to the connector's
    own API — so a user's app needs NO core-code changes, just a manifest.
    """
    aid = action["id"]
    name = f"{cid}_{aid}"
    # json.dumps, not .replace("'", "\\'"): a JSON string literal is also a valid
    # JS one, and it escapes newlines, backslashes and quotes. Hand-escaping only
    # the apostrophe meant a multi-line YAML `description:` emitted an
    # unterminated JS string — invalid .mjs that fails inside the agent sandbox,
    # where nobody sees the syntax error. Emits its own quotes.
    desc = _json.dumps(action.get("description") or f"{aid} via the {cid} connector")
    props = action.get("input") or {}
    schema = {"type": "object", "properties": props, "additionalProperties": False}
    return f"""// AUTO-GENERATED from connectors/{cid}/connector.yaml (action: {aid}).
// Regenerate with:  ava connector tools {cid} --write
const BRIDGE = process.env.AVA_BRIDGE_URL || '{_layout.bridge_url(_bridge_port())}';

export default {{
  name: '{name}',
  description: {desc},
  inputSchema: {_json.dumps(schema, indent=2)},
  async handler(args, ctx) {{
    try {{
      // Through the sandbox guard proxy (ctx.http = _lib.mjs curl helpers), the
      // way every core tool reaches the bridge. Node's built-in fetch ignores
      // the proxy and the sandbox refuses the direct connection, so the tool
      // failed on every call while the same request from a shell worked.
      const data = await ctx.http.postJson(`${{BRIDGE}}/internal/connector/{cid}/{aid}`, args || {{}}, {{
        direct: false, timeout: 150,
        headers: {{ 'X-Ava-Internal-Token': ctx.internalToken || '' }},
      }});
      return typeof data === 'string' ? data : JSON.stringify(data, null, 2);
    }} catch (e) {{
      return `Error calling {name}: ${{e.message}}`;
    }}
  }},
}};
"""


# --- Meta tools (dynamic tool loading) ----------------------------------------
# Past ~15 tools, per-action schemas bloat the agent's context on every turn and
# tool selection degrades. Large static connectors therefore swap N generated
# tools for TWO meta tools — <cid>_find_tool (search the action set) and
# <cid>_call (invoke one by name) — the same shape mcp/discover connectors use,
# feeding the same policed __tools/__call bridge routes and approvals gate.
META_TOOLS_MIN = 16


def meta_static(m: dict) -> bool:
    """True when a static connector is big enough to use find/call meta tools."""
    return len([a for a in _static_actions(m)
                if a.get("id") and a.get("path")]) >= META_TOOLS_MIN


def _static_tool_schemas(m: dict) -> list[dict]:
    """Static generic-proxy actions in discovered-tool shape, so __tools serves
    a large static connector exactly like an MCP/discover one."""
    out = []
    for a in _static_actions(m):
        if not (a.get("id") and a.get("path")):
            continue
        method = str(a.get("method") or "POST").upper()
        desc = (a.get("description") or "").strip()
        out.append({"name": a["id"],
                    "description": f"[{method} {a.get('path')}] {desc}".strip(),
                    "inputSchema": {"type": "object",
                                    "properties": a.get("input") or {}},
                    "access": _infer_access(a)})
    return out


def _filter_tools(res: dict, query: str, limit: int) -> dict:
    """Keyword-filter and cap a {"tools": [...]} result (find_tool's search).
    `total` reports the pre-cap match count so the agent knows to refine."""
    tools = res.get("tools") if isinstance(res, dict) else None
    if not isinstance(tools, list):
        return res
    terms = [t for t in str(query or "").lower().split() if t]
    if terms:
        def hits(t: dict) -> int:
            blob = f"{t.get('name', '')} {t.get('description', '')}".lower()
            return sum(1 for term in terms if term in blob)
        tools = sorted((t for t in tools if hits(t)), key=hits, reverse=True)
    total = len(tools)
    if limit and limit > 0:
        tools = tools[:limit]
    return {**res, "tools": tools, "total": total}


def render_find_tool(cid: str) -> str:
    """The .mjs source for <cid>_find_tool: search the connector's tool set."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    label = m.get("label") or cid
    acts = [a for a in _static_actions(m) if a.get("id") and a.get("path")]
    if acts:
        caps = sorted({action_capability(a) for a in acts})
        hint = f"{len(acts)} actions across: {', '.join(caps[:12])}"
    else:
        hint = "its tools are discovered live"
    # json.dumps escapes the whole string (see render_tool) and emits its own
    # quotes, so `label` needs no hand-escaping and the apostrophe is literal.
    # A label that already ends in "App" ("Health App") must not render as
    # "the Health App app's" — refer to it by the label alone in that case.
    app_ref = label if label.lower().endswith(" app") else f"{label} app"
    desc = _json.dumps(
        f"Search the {app_ref}'s available actions ({hint}). "
        f"ALWAYS call this first with a few keywords for what you want to do, "
        f"then invoke the chosen action with {cid}_call.")
    return f"""// AUTO-GENERATED from connectors/{cid}/connector.yaml (meta: find_tool).
// Regenerate with:  ava connector tools {cid} --write
const BRIDGE = process.env.AVA_BRIDGE_URL || '{_layout.bridge_url(_bridge_port())}';

export default {{
  name: '{cid}_find_tool',
  description: {desc},
  inputSchema: {{
    type: 'object',
    properties: {{
      query: {{ type: 'string', description: 'keywords for the action you need (e.g. "create persona")' }},
      limit: {{ type: 'number', description: 'max results (default 12)' }},
    }},
    additionalProperties: false,
  }},
  async handler(args, ctx) {{
    try {{
      const q = encodeURIComponent(args.query || '');
      const n = Number(args.limit) > 0 ? Number(args.limit) : 12;
      // Through the sandbox guard proxy (ctx.http = _lib.mjs curl helpers), the
      // way every core tool reaches the bridge — Node's built-in fetch is refused there.
      const data = await ctx.http.getJson(`${{BRIDGE}}/internal/connector/{cid}/__tools?q=${{q}}&limit=${{n}}`, {{
        direct: false, timeout: 30,
        headers: {{ 'X-Ava-Internal-Token': ctx.internalToken || '' }},
      }});
      return JSON.stringify(data, null, 2);
    }} catch (e) {{
      return `Error calling {cid}_find_tool: ${{e.message}}`;
    }}
  }},
}};
"""


def render_call_tool(cid: str) -> str:
    """The .mjs source for <cid>_call: invoke one tool found via find_tool."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    # See render_tool: json.dumps escapes the whole string and emits its quotes.
    desc = _json.dumps(
        f"Run one {m.get('label') or cid} action by name — find the name and its "
        f"input schema with {cid}_find_tool first.")
    return f"""// AUTO-GENERATED from connectors/{cid}/connector.yaml (meta: call).
// Regenerate with:  ava connector tools {cid} --write
const BRIDGE = process.env.AVA_BRIDGE_URL || '{_layout.bridge_url(_bridge_port())}';

export default {{
  name: '{cid}_call',
  description: {desc},
  inputSchema: {{
    type: 'object',
    properties: {{
      name: {{ type: 'string', description: 'the action name returned by {cid}_find_tool' }},
      arguments: {{ type: 'object', description: 'arguments matching that action\\'s inputSchema' }},
    }},
    required: ['name'],
    additionalProperties: false,
  }},
  async handler(args, ctx) {{
    try {{
      // Through the sandbox guard proxy (ctx.http = _lib.mjs curl helpers), the
      // way every core tool reaches the bridge — Node's built-in fetch is refused there.
      // The timeout outlasts the approvals gate (120s): a call parked for the
      // owner's decision must come back with the verdict, not a curl timeout.
      const data = await ctx.http.postJson(`${{BRIDGE}}/internal/connector/{cid}/__call`,
        {{ name: args.name, arguments: args.arguments || {{}} }}, {{
        direct: false, timeout: 150,
        headers: {{ 'X-Ava-Internal-Token': ctx.internalToken || '' }},
      }});
      return typeof data === 'string' ? data : JSON.stringify(data, null, 2);
    }} catch (e) {{
      return `Error calling {cid}_call: ${{e.message}}`;
    }}
  }},
}};
"""


_undeployed_cache: Dict[str, object] = {"ts": 0.0, "list": None}


def undeployed() -> List[dict]:
    """Enabled connectors with an agent surface whose generated tools are NOT
    on disk (the Hub's "tools stale" state — so definitely not in the sandbox).
    Feeds the chat-turn awareness note: Ava must tell the user to Deploy rather
    than shrug or pretend. Cached ~30s; tool_files renders per connector."""
    now = time.time()
    if _undeployed_cache["list"] is not None and now - float(_undeployed_cache["ts"]) < 30:
        return _undeployed_cache["list"]  # type: ignore[return-value]
    root = settings.connector_tools_dir()
    out = []
    for m in load():
        files = tool_files(m["id"])
        if not files:
            continue
        # NB: an explicit loop — this module defines all() (the registry
        # accessor), which shadows the builtin.
        for t in files:
            if not os.path.exists(os.path.join(root, m["id"], t["name"])):
                out.append({"id": m["id"], "label": m.get("label", m["id"]),
                            "tools": len(files)})
                break
    _undeployed_cache.update(ts=now, list=out)
    return out


def _split_trailing_comment(rest: str) -> tuple:
    """Split a YAML value region into `(value, comment)`.

    A naive `[^#]*` split is wrong on the most common appearance edit there is:
    `color: "#abcdef"` has a `#` INSIDE a quoted scalar, and treating it as a
    comment corrupts the line. YAML only starts a comment at a `#` that is
    outside quotes AND preceded by whitespace (`a: b#c` is the string `b#c`).
    """
    quote = ""
    for i, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t"):
            return rest[:i], rest[i:]
    return rest, ""


#: Sentinel for `patch_manifest_text`: remove the key rather than set it. The
#: appearance route CLEARS `ui.icon` back to the frontend's auto-pick, and an
#: explicit `icon: null` is not the same manifest as one with no icon at all.
REMOVE = object()


def patch_manifest_text(text: str, keys: tuple, value) -> str | None:
    """Set ONE scalar key in a manifest's text, leaving every other byte alone.

    Returns the new text, or `None` when the edit cannot be made confidently —
    the caller then falls back to a full `safe_dump`, which is correct but throws
    the file's comments away.

    Why this exists. The Hub's enable/disable and appearance routes round-tripped
    the whole manifest through the YAML emitter, so ticking a checkbox rewrote the
    owner's hand-written file and deleted every comment and blank line in it. That
    contradicts the loader's own rule — "the owner asked for that YAML; a loader
    that 'repairs' it loses whatever they were in the middle of writing" — and it
    is worse for a connector than for most files, because the comments in one are
    usually the notes on what the app's actions do.

    Handles a top-level scalar (`enabled`) and one nested level (`ui.icon`), which
    is every key these routes touch. Anything deeper, ambiguous, or already
    holding a non-scalar bails rather than guessing.

    **The edit is verified by round-trip before it is returned**: the patched text
    is re-parsed and compared against the original parse with only the target key
    changed. A surgical text edit that did anything unexpected is discarded, so
    the worst case is the old lossy behaviour and never a corrupted manifest.
    """
    if not keys or len(keys) > 2:
        return None
    try:
        before = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 — an unparsable file is the caller's problem
        return None
    if not isinstance(before, dict):
        return None

    rendered = None if value is REMOVE else _json.dumps(value)
    lines = text.splitlines(keepends=True)

    def _indent(ln: str) -> int:
        return len(ln) - len(ln.lstrip(" "))

    def _is_blank(ln: str) -> bool:
        return not ln.strip() or ln.lstrip().startswith("#")

    # Locate the block the leaf lives in: the whole file, or a `parent:` mapping.
    lo, hi, want_indent = 0, len(lines), 0
    if len(keys) == 2:
        opens = [i for i, ln in enumerate(lines)
                 if _re.match(rf"^{keys[0]}:\s*(#.*)?$", ln)]
        if len(opens) != 1:
            return None                     # absent or ambiguous — do not guess
        lo = opens[0] + 1
        hi = lo
        while hi < len(lines) and (_is_blank(lines[hi]) or _indent(lines[hi]) > 0):
            hi += 1
        want_indent = 2
        for ln in lines[lo:hi]:             # match the block's own indent
            if not _is_blank(ln):
                want_indent = _indent(ln)
                break

    leaf = keys[-1]
    hits = [i for i in range(lo, hi)
            if _re.match(rf"^ {{{want_indent}}}{_re.escape(leaf)}:", lines[i])]
    if len(hits) > 1:
        return None                         # duplicated key: ambiguous
    if hits:
        i = hits[0]
        m = _re.match(rf"^( {{{want_indent}}}{_re.escape(leaf)}:)(.*?)(\r?\n)?$",
                      lines[i])
        if not m:
            return None
        existing, comment = _split_trailing_comment(m.group(2) or "")
        if not existing.strip() and not comment:
            return None                     # a block value, not a scalar
        if rendered is None:
            del lines[i]                    # REMOVE: the whole line goes
        else:
            # Keep the owner's own spacing before a trailing comment — aligned
            # comments down a column are the reason they are readable.
            gap = existing[len(existing.rstrip()):] if comment else ""
            lines[i] = (f"{m.group(1)} {rendered}{gap}{comment}{m.group(3) or ''}")
    elif rendered is None:
        return text                         # already absent — nothing to do
    else:
        pad = " " * want_indent
        at = hi
        while at > lo and _is_blank(lines[at - 1]):
            at -= 1                         # before the block's trailing blanks
        if at and not lines[at - 1].endswith("\n"):
            lines[at - 1] += "\n"
        lines.insert(at, f"{pad}{leaf}: {rendered}\n")

    patched = "".join(lines)
    try:
        after = yaml.safe_load(patched)
    except Exception:  # noqa: BLE001
        return None
    expected = _copy.deepcopy(before)
    if len(keys) == 2:
        if not isinstance(expected.get(keys[0]), dict):
            return None
        if value is REMOVE:
            expected[keys[0]].pop(keys[1], None)
        else:
            expected[keys[0]][keys[1]] = value
    elif value is REMOVE:
        expected.pop(keys[0], None)
    else:
        expected[keys[0]] = value
    return patched if after == expected else None


def orphans() -> Dict[str, List[str]]:
    """Generated agent material with NO connector behind it any more.

    The reverse of `undeployed()`, and the direction nothing looked in. Every
    check in the system walks what the manifests declare and asks whether it
    reached the sandbox; none walked what is on disk and asked whether anything
    still declares it. So a connector removed by hand — or deleted before the
    delete path cleaned up after itself — left its rendered tools behind, and
    `install.sh` tars the whole tree, so they ship into the sandbox on the next
    provision. Ava is then handed tools whose `/internal/connector/<cid>/*`
    routes no policy allows, which deny-by-default guarantees will fail: not a
    missing capability, a broken one.

    Keyed on `catalog()`, not `load()`, because a DISABLED connector still has a
    manifest and its generated files are not orphaned — just dormant.
    """
    known = {x["id"] for x in catalog()}
    tools: List[str] = []
    policies: List[str] = []
    try:
        root = settings.connector_tools_dir()
        for name in sorted(os.listdir(root)):
            if name.startswith((".", "_")) or name in known:
                continue
            if os.path.isdir(os.path.join(root, name)):
                tools.append(name)
    except OSError:
        pass
    try:
        pdir = settings.generated_policy_dir()
        for fn in sorted(os.listdir(pdir)):
            # The FILE STEM is the connector id — `preset.name` deliberately is
            # not (see policy_inventory), so the stem is the only join key back
            # to connectors/<id>/connector.yaml.
            if fn.endswith(".yaml") and fn[:-5] not in known:
                policies.append(fn[:-5])
    except OSError:
        pass
    # Stale FILES inside a live connector's tools dir: renaming an action (or
    # crossing the meta-tool threshold) writes the new .mjs set and leaves the
    # old files, and install.sh tars the whole dir — the sandbox is handed
    # phantom tools whose routes no policy allows. Expected names are derived
    # from catalog() (a disabled connector's files are dormant, not stale).
    stale_files: List[str] = []
    by_id = {x["id"]: x for x in catalog()}
    try:
        root = settings.connector_tools_dir()
        for cid, m in by_id.items():
            d = os.path.join(root, cid)
            if not os.path.isdir(d):
                continue
            expected = {t["name"] for t in _tool_file_names(m)}
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".mjs") and fn not in expected:
                    stale_files.append(f"{cid}/{fn}")
    except OSError:
        pass
    # Per-connector state with no manifest behind it: standing grants and the
    # self-reported tier cache both key on the id, and ids are reusable — a
    # leftover entry silently pre-approves whatever app takes the name next.
    from . import grants as _grants
    from . import tools_cache as _tools_cache
    stale_grants = sorted(set(_grants.known_ids()) - set(by_id))
    stale_cache = sorted(set(_tools_cache.known_ids()) - set(by_id))
    return {"tools": tools, "policies": policies, "tool_files": stale_files,
            "grants": stale_grants, "cache": stale_cache}


def _tool_file_names(m: dict) -> List[dict]:
    """`tool_files` without rendering sources — names only, from any manifest
    (including a disabled one, which `tool_files`'s `load()` cannot see)."""
    cid = m["id"]
    if _discover_spec(m) or _mcp_spec(m) or meta_static(m):
        return [{"name": f"{cid}_find_tool.mjs"}, {"name": f"{cid}_call.mjs"}]
    return [{"name": f"{cid}_{a['id']}.mjs"}
            for a in _static_actions(m) if a.get("id") and a.get("path")]


def prune_orphans(write: bool = False) -> Dict[str, List[str]]:
    """Remove what `orphans()` found. `write=False` reports without touching.

    Deliberately explicit rather than automatic on provision: this deletes files,
    and a provision run that quietly removed things the owner had not asked about
    is exactly the kind of surprise the audit ledger exists to make impossible.
    """
    from . import audit
    found = orphans()
    if not write:
        return found
    removed: Dict[str, List[str]] = {"tools": [], "policies": [],
                                     "tool_files": [], "grants": [], "cache": []}
    for cid in found["tools"]:
        try:
            _shutil.rmtree(os.path.join(settings.connector_tools_dir(), cid))
            removed["tools"].append(cid)
        except OSError:
            pass
    for cid in found["policies"]:
        try:
            os.remove(os.path.join(settings.generated_policy_dir(), f"{cid}.yaml"))
            removed["policies"].append(cid)
        except OSError:
            pass
    for rel in found.get("tool_files", []):
        try:
            os.remove(os.path.join(settings.connector_tools_dir(), *rel.split("/", 1)))
            removed["tool_files"].append(rel)
        except OSError:
            pass
    from . import grants as _grants
    from . import tools_cache as _tools_cache
    for cid in found.get("grants", []):
        if _grants.forget(cid):
            removed["grants"].append(cid)
    for cid in found.get("cache", []):
        if _tools_cache.forget(cid):
            removed["cache"].append(cid)
    if any(removed.values()):
        # An egress policy going is a change to what the sandbox may reach, so it
        # belongs in the ledger for the same reason a connector delete does.
        audit.record("connector_prune", tools=removed["tools"],
                     policies=removed["policies"],
                     tool_files=removed["tool_files"],
                     grants=removed["grants"], cache=removed["cache"])
    return removed


def tool_files(cid: str) -> List[dict]:
    """The canonical agent-tool set for a connector: find/call meta tools for
    dynamic (mcp/discover) or large static connectors, else one tool per
    generic-proxy action. Every generator/checker derives from this ONE list."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    if _discover_spec(m) or _mcp_spec(m) or meta_static(m):
        return [{"name": f"{cid}_find_tool.mjs", "source": render_find_tool(cid)},
                {"name": f"{cid}_call.mjs", "source": render_call_tool(cid)}]
    return [{"name": f"{cid}_{a['id']}.mjs", "source": render_tool(cid, a)}
            for a in _static_actions(m) if a.get("id") and a.get("path")]


def agent_surface() -> List[dict]:
    """How the agent reaches each connected app — the persona's connected-apps
    block (`agent/render_persona.py`, `{{APPS_BLOCK}}`) is rendered from this.

    One row per ENABLED `kind: app` connector that exposes tools at all:

        {"id": cid, "label": <owner-facing name>,
         "meta": bool,          # True: reached via <id>_find_tool -> <id>_call
                                # False: one native tool per action, <id>_<action>
         "tools": [names],      # what the model may route to (see below)
         "discovered": bool}    # tools come from the app's own tool list

    `meta` follows `tool_files` exactly — dynamic (discover facade or `mcp:`)
    or a static set at or above META_TOOLS_MIN — so the persona never names a
    tool shape the sandbox does not hold. `tools` is the declared action ids
    for a static connector, or the names the app reported on its last
    discovery (`tools_cache`) for a dynamic one: empty until the first
    discovery, which is honest rather than a guess, and sorted so the persona
    digest (`provision.persona_digest`) only moves when the set does. Tier
    and description are deliberately absent — the block is a routing index,
    not a permissions sheet.
    """
    out: List[dict] = []
    for m in load():
        if m.get("kind") != "app":
            continue
        cid = m["id"]
        static = [str(a["id"]) for a in _static_actions(m)
                  if a.get("id") and a.get("path")]
        dynamic = bool(_discover_spec(m) or _mcp_spec(m))
        if not static and not dynamic:
            continue
        if dynamic:
            from . import tools_cache
            names = sorted(tools_cache.for_connector(cid))
        else:
            names = static
        out.append({"id": cid, "label": _display_label(m),
                    "meta": dynamic or meta_static(m),
                    "tools": names, "discovered": dynamic})
    return out


# --- App surface (data-driven nav) ------------------------------------------
# A connector with a `ui:` block appears in Ava's left rail. `embed` selects how
# the shell renders it:
#   native  — a first-party React view compiled into the bundle (view= registry key)
#   iframe  — the app serves its own web UI; Ava reverse-proxies it same-origin
#             under /apps/<id>/ so it inherits the session cookie
#   none    — no UI; Ava renders an action console instead (ActionConsole.tsx,
#             fed by app_actions() — the app's RESOLVED tool list, not the
#             manifest's, because for a dynamic connector the manifest holds one
#             synthetic bridge row and that is not a usable answer)
def _safe_order(v) -> int:
    """`ui.order` as an int, whatever the manifest held. `_validate_nested`
    already coerces or quarantines it, but `apps()` renders the whole left
    rail — one non-comparable value in a SORT key blanks every app, so the
    consumer stays safe even if a future path skips validation."""
    if isinstance(v, bool) or v is None:
        return 100
    try:
        return int(v)
    except (TypeError, ValueError):
        return 100


#: Where the owner's hand-arranged sidebar order lives. A LIST of app ids in
#: display order, held in ava.yaml next to `ui.tours_seen` — server-side for the
#: same reason the walkthrough state is (see hub/tour.py): Ava is single-user by
#: construction, but it is also a PWA opened from a phone as well as a desktop,
#: and localStorage is per-browser-profile. An arrangement the owner made once
#: has to survive the second device and a cache clear.
#:
#: NOT `ui.order` in each manifest. That field is the AUTHOR's default, and two
#: things rule it out as the owner's arrangement: a built-in connector's
#: manifest is read-only from the UI (`set_connector_appearance` refuses one
#: outright), so half the rail could never be dragged; and one drag rewrites
#: every app's position, which is one list, not N independent facts.
ORDER_KEY = "ui.app_order"


def saved_order() -> List[str]:
    """The owner's hand-arranged app order — ids only, junk dropped.

    Read at REQUEST time rather than snapshotted at import, mirroring
    `hub/tour.seen()`: settings loads config once at import, so a cached copy
    would need a bridge restart before a drag took effect.
    """
    raw = settings.get(ORDER_KEY, []) or []
    if not isinstance(raw, list):
        return []
    seen, out = set(), []
    for x in raw:
        cid = str(x).strip()
        # Deduped because a repeated id in the stored list would give one app two
        # positions, and `.index()` below would silently honour the first.
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def save_order(ids: List[str]) -> List[str]:
    """Persist the owner's arrangement. Keeps the key name in ONE place — the
    caller passes ids, never a settings path."""
    settings.save_patch({"ui": {ORDER_KEY.split(".", 1)[1]: list(ids)}})
    return list(ids)


def apps() -> List[dict]:
    """Nav entries for connectors that declare a `ui:` block, sorted for display.

    Order: core section first, then the owner's saved arrangement, then the
    manifest's `ui.order`, then the id. An app the owner has never dragged has
    no saved position and sorts AFTER the ones they have — appended to the end
    rather than dropped into the middle of an arrangement it was never part of.
    """
    order = saved_order()
    out = []
    for m in load():
        ui = m.get("ui")
        if not isinstance(ui, dict):
            continue
        embed = str(ui.get("embed") or "none").lower()
        out.append({
            "id": m["id"],
            "label": _display_label(m),
            # None (not "grid") when undeclared: the frontend's appIcon() then
            # picks a STABLE per-app glyph by hashing the id, so apps that don't
            # declare ui.icon still differ from each other — mirrors how
            # appAccent() varies the color. Passing "grid" here would defeat that.
            "icon": ui.get("icon") or None,
            "color": str(ui["color"]) if ui.get("color") else None,
            "section": str(ui.get("section") or "apps"),
            "order": _safe_order(ui.get("order")),
            "embed": embed,
            "view": ui.get("view"),
            "url": f"/apps/{m['id']}/" if embed == "iframe" else None,
            "has_api": bool(ui.get("api")),
        })
    # `len(order)` for an app with no saved position: a stable "after everything
    # placed by hand", without special-casing the comparison.
    out.sort(key=lambda a: (
        0 if a["section"] == "core" else 1,
        order.index(a["id"]) if a["id"] in order else len(order),
        a["order"], a["id"]))
    return out


def _display_label(m: dict) -> str:
    """`ui.label`, else the manifest label, else the id — in one place.

    Both `apps()` (nav) and `app()` (proxy) need the owner-facing name, and a
    second copy of this chain is how one of them ends up showing a bare id.
    """
    ui = m.get("ui") if isinstance(m.get("ui"), dict) else {}
    return ui.get("label") or m.get("label") or m["id"]


def app(cid: str) -> dict | None:
    """A connector's `ui:` block, url-expanded — for the same-origin iframe proxy."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    ui = m.get("ui")
    if not isinstance(ui, dict):
        return None
    # `label` is here so a proxy error can NAME the app. Without it the error
    # path fell back to the bare id — "home-assistant isn't running" where every
    # other surface says "Home Assistant".
    return {"id": cid, "label": _display_label(m),
            "embed": str(ui.get("embed") or "none").lower(),
            "url": _expand(ui.get("url")), "api": ui.get("api")}


def app_api(cid: str) -> dict | None:
    """Resolved browser data-proxy config {base, prefix, token} from `ui.api`,
    or None if the connector declares no browser API. Powers /apps/<id>/api/*.
    """
    api = (({x["id"]: x for x in load()}.get(cid) or {}).get("ui") or {}).get("api")
    if not isinstance(api, dict):
        return None
    base = _expand(api.get("base")) or base_url(cid)  # default to the probe host
    if not base:
        return None
    token = (settings.env_secret(api["token_env"]) or "") if api.get("token_env") else ""
    return {"base": base.rstrip("/"),
            "prefix": str(api.get("prefix") or "").rstrip("/"),
            "token": token}


def app_token(cid: str) -> str:
    """The saved credential Ava presents to an embedded app on the owner's behalf,
    resolved from the connector's declared ``token_env`` (any manifest shape, see
    ``auth_env``) via ``settings.env_secret``. ``''`` when the app declares no
    credential or none is saved yet.

    This is what lets "connect the app once" mean the human never re-authenticates
    to the app's own UI: the same-origin app proxy injects this as the bearer when
    the browser sends none (a fresh embed has no app session of its own). Resolved
    only here on the bridge when building the egress request — never handed to the
    browser or inherited by the sandboxed agent (Ava-never-has-passwords).

    Resolves through ``proxy_token_env``, NOT ``auth_env``: only a credential the
    manifest states is the app's own may be presented to the app. An ``mcp:``
    server's token is not, and forwarding it would hand a token issued by one host
    to another."""
    m = {x["id"]: x for x in load()}.get(cid)
    if not m:
        return ""
    name = proxy_token_env(m)
    return (settings.env_secret(name) or "") if name else ""


# --- MCP servers -------------------------------------------------------------
# Wrap ANY Model Context Protocol server as a connector: the bridge speaks real
# MCP (JSON-RPC over Streamable HTTP or stdio, see ava_bridge/mcp_client.py),
# while the sandboxed agent reaches only the policed __tools/__call bridge
# routes its auto-generated egress policy allow-lists. Manifest form:
#   mcp:
#     url: "http://127.0.0.1:9200/mcp"      # http transport
#     token_env: MYMCP_TOKEN                 # optional bearer
#     # transport: sse                       # legacy HTTP+SSE (e.g. Home
#     #                                      # Assistant's MCP Server); inferred
#     #                                      # when the url path ends in /sse
#     # or a stdio server (spawned by the bridge, host-side):
#     command: ["npx", "-y", "@modelcontextprotocol/server-github"]
#     env: { GITHUB_TOKEN: "${GITHUB_TOKEN}" }
def _mcp_spec(m: dict) -> dict | None:
    """A connector's MCP-server spec, or None."""
    mcp = m.get("mcp")
    if not isinstance(mcp, dict):
        return None
    url = _expand(mcp.get("url") or "") or None
    if url and not url.startswith(("http://", "https://")):
        url = None      # a ${VAR} left unset — the manifest is inert until configured
    command = mcp.get("command")
    if isinstance(command, str):
        command = command.split()
    if not url and not command:
        return None
    transport = mcp.get("transport") or (
        "stdio" if command and not url
        else "sse" if url and url.split("?")[0].rstrip("/").endswith("/sse")
        else "http")
    env = {k: _expand(str(v)) for k, v in (mcp.get("env") or {}).items()} or None
    return {"transport": transport, "url": url, "command": command,
            "env": env, "token_env": mcp.get("token_env"),
            # Container isolation for a stdio server (filesystem-contained):
            # sandbox: docker · image: <base with the runtime> · network: none|bridge
            "sandbox": str(mcp.get("sandbox") or "none").lower(),
            "image": mcp.get("image") or "node:20-slim",
            "network": str(mcp.get("network") or "bridge")}


# --- Why a connector request failed -----------------------------------------
# "Unreachable" is four different conditions wearing one word, and the owner's
# next move differs for each. Connection REFUSED is the one worth separating:
# the address is right, the machine answered, and nothing is listening — the app
# is not running. That is a `start it` problem, not a `something is broken`
# problem, and every other failure here is genuinely the latter.
#
# Before this, all four call sites rendered `{e}` and the owner got
#   HTTPConnectionPool(host='127.0.0.1', port=8479): Max retries exceeded with
#   url: /?theme=dark&embedded=1&v=1 (Caused by NewConnectionError(...))
# which names the library, the retry policy and the query string — everything
# except the sentence "<the app> isn't running".
#
# The codes follow the feature-registry convention (docs/CONNECTOR_SDK.md §6):
# `<cid>_down` already resolves to the Operations fix-it link via the `_down`
# PATTERN in frontend/src/lib/fixes.ts, so a connector gets that link with zero
# frontend changes — which is exactly where a stopped app should send you, since
# its `service.probe` health row lives there.
def _request_wrappers() -> tuple:
    """requests' own exceptions subclass IOError (== OSError), so a naive
    isinstance check matches the OUTER wrapper and never reaches the errno
    underneath. Skip them while walking. Imported lazily — `requests` is a
    lazy import everywhere else in this module."""
    try:
        import requests.exceptions as rex
        return (rex.RequestException,)
    except Exception:  # noqa: BLE001
        return ()


def _errno_cause(e: BaseException) -> BaseException | None:
    """The innermost real OSError in an exception chain, or None.

    requests wraps the actual errno three deep — ConnectionError ->
    MaxRetryError -> NewConnectionError -> ConnectionRefusedError — and only
    that last one carries the fact we want. We walk `__cause__`/`__context__`
    rather than string-matching "Connection refused", which is locale- and
    platform-shaped; ConnectionRefusedError is a builtin and means the same
    thing on every platform.
    """
    wrappers = _request_wrappers()
    seen, cur, depth = set(), e, 0
    while cur is not None and depth < 10:
        if id(cur) in seen:  # defensive: chains can be cyclic
            break
        seen.add(id(cur))
        if isinstance(cur, OSError) and not isinstance(cur, wrappers):
            return cur
        cur = cur.__cause__ or cur.__context__
        depth += 1
    return None


def unreachable(cid: str, e: Exception, *, url: str = "", what: str = "app") -> dict:
    """Classify a failed connector request -> {"error", "error_code", "detail"}.

    `error` is a finished sentence for the owner (and for the agent, which
    reads these verbatim — the self-contained-message exception in CLAUDE.md).
    `detail` keeps the raw exception so nothing is lost for debugging.
    """
    import socket

    label = (app(cid) or {}).get("label") or cid
    where = _netloc(url)
    root = _errno_cause(e)
    name = type(e).__name__

    # Refused: the host answered, nothing is bound to the port. Not running.
    if isinstance(root, ConnectionRefusedError):
        at = f" on {where}" if where else ""
        return {"error": f"{label} isn't running — nothing is listening{at}. "
                         f"Start the app, then reload.",
                "error_code": f"{cid}_down", "detail": str(e)}
    # Timeout checked BEFORE the generic reachability branch: a ReadTimeout is a
    # requests.ConnectionError subclass in some versions, and "connected but
    # silent" is a different owner action from "wrong address".
    if "Timeout" in name:
        return {"error": f"{label} accepted the connection but didn't answer in time — "
                         f"it's running, but not responding.",
                "error_code": f"{cid}_timeout", "detail": str(e)}
    # No route, DNS miss, connection torn down mid-flight: the address itself is
    # suspect, so send the owner to the manifest rather than to "start the app".
    if isinstance(root, (socket.gaierror, ConnectionAbortedError, ConnectionResetError)) \
            or name == "ConnectionError":
        return {"error": f"{label} can't be reached at {where or 'its configured address'} — "
                         f"check the address in its connector manifest.",
                "error_code": f"{cid}_unreachable", "detail": str(e)}
    return {"error": f"{label} {what} request failed: {e}",
            "error_code": f"{cid}_error", "detail": str(e)}


def _netloc(url: str) -> str:
    from urllib.parse import urlparse
    if not url:
        return ""
    try:
        return urlparse(url).netloc or ""
    except Exception:  # noqa: BLE001
        return ""


# --- Dynamic tool discovery -------------------------------------------------
# For apps that expose an MCP-style list+call API (e.g. a FastMCP facade). Ava's
# bridge lists the app's tools and forwards calls, so a whole dynamic tool set is
# bridged from a manifest with no per-tool wiring. Real MCP servers use the
# `mcp:` block above instead; both feed the same __tools/__call bridge routes.
def _discover_base(cid: str, spec: dict) -> str | None:
    return (spec.get("base") or base_url(cid) or "").rstrip("/") or None


def _discover_headers(spec: dict) -> dict:
    h = {"Content-Type": "application/json"}
    tok = settings.env_secret(spec.get("token_env"))
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


def discover_tools(cid: str, query: str = "", limit: int = 0) -> dict:
    """The connector's dynamic tool list -> {"tools": [...]} or {"error"}.
    Routes to the real-MCP client when the manifest declares `mcp:`, else the
    HTTP list+call discover facade, else (a large static connector's find_tool)
    the manifest's own actions. `query`/`limit` filter the result server-side
    so the agent's find_tool returns a shortlist, not the whole set."""
    m = {x["id"]: x for x in load()}.get(cid) or {}

    def _remember(res: dict) -> dict:
        """Write the declared access tiers through to the JIT cache (best-effort
        — discovery must never fail on cache IO) before any query filtering."""
        try:
            tools = res.get("tools") if isinstance(res, dict) else None
            if isinstance(tools, list):
                from . import tools_cache
                tools_cache.update(cid, tools)
        except Exception:  # noqa: BLE001
            pass
        return res

    mcp = _mcp_spec(m)
    if mcp:
        from . import mcp_client
        return _filter_tools(_remember(mcp_client.list_tools(cid, mcp)), query, limit)
    spec = _discover_spec(m)
    if not spec:
        if _static_actions(m):
            return _filter_tools({"tools": _static_tool_schemas(m)}, query, limit)
        return {"error": f"connector {cid} declares no discover spec"}
    base = _discover_base(cid, spec)
    if not base:
        return {"error": f"connector {cid} has no discover base_url"}
    import requests
    try:
        r = requests.get(base + spec["list"], headers=_discover_headers(spec), timeout=20,
                         allow_redirects=False)
        # Status FIRST. This used to go straight to r.json() and only report an
        # error when the PARSE failed — but a FastAPI 404 body is `{"detail":"Not
        # Found"}`, which parses perfectly. So pointing a facade connector at a
        # server with no /tools route returned ok/verified with zero tools, and the
        # endpoint whose docstring promises "only when Ava genuinely spoke MCP to it
        # a moment ago" reported verified:true for a handshake that 404'd.
        if not (200 <= r.status_code < 300):
            return {"error": f"{cid} discovery returned HTTP {r.status_code}"}
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            return {"error": f"{cid} discovery returned non-JSON "
                             f"({(r.headers.get('Content-Type') or '?').split(';')[0]})"}
        # A 200 that is not the facade's shape is not a discovery either. Without
        # this, any JSON object counts as success and the tool list is silently [].
        if not isinstance(body, dict) or not isinstance(body.get("tools"), list):
            return {"error": f"{cid} discovery returned no tools list "
                             f"(got keys: {sorted(body)[:5] if isinstance(body, dict) else type(body).__name__})"}
        return _filter_tools(_remember(body), query, limit)
    except Exception as e:  # noqa: BLE001
        return unreachable(cid, e, url=base, what="discovery")


def app_actions(cid: str, live: bool = True) -> dict:
    """One connector's agent surface RESOLVED rather than declared -- what
    ``GET /api/apps/{cid}/actions`` renders in the ActionConsole.

    ``actions()`` answers a different question: what the manifest spells. For a
    dynamic connector that is one synthetic bridge row, which tells the owner
    nothing about what the app can actually do -- and for an ``ui.embed: none``
    app the console is the whole tile, so "nothing" is the entire product.

    ``source`` is why the list looks the way it does, so an empty one is never
    ambiguous:

        live      the app answered just now -- MCP ``tools/list``, or the
                  ava-tools/1 facade's ``/tools``
        cache     it did not; this is the last discovery that succeeded
                  (``tools_cache``), and ``error`` says why the live one failed
        declared  the manifest's own static actions; no network involved
        none      nothing to show -- and ``error`` separates "declares no agent
                  surface" from "could not ask", which are the owner's problem
                  and the app's problem respectively

    A reachable app that lists zero tools is ``live`` with an empty list, NOT
    ``none``: "it answered and has none" and "it never answered" send someone to
    different places, and collapsing them is the bug this whole function exists
    to stop repeating.

    Each row carries the tier Ava will ACTUALLY enforce (``action_access``,
    which lays the manifest's ``dynamic_access`` patterns over the app's own
    self-report) and whether it asks first (``needs_confirm``) -- never the raw
    claim. A console showing the self-report would promise ``read`` for a tool
    Ava is going to prompt on, which is the one lie a permissions surface must
    not tell.

    A failed discovery is DATA here, never an exception -- including one raised
    rather than returned. The console is the only view of an app whose UI does
    not exist, so a 500 leaves the owner with a blank panel and no way to tell
    blank from broken, which is the exact confusion this function exists to end.
    """
    m = {x["id"]: x for x in load()}.get(cid) or {}
    if not m:
        return {"connector": cid, "label": cid, "transport": "none",
                "source": "none", "tools": [],
                "error": f"unknown connector '{cid}'"}

    def _row(name: str, description: str) -> dict:
        # 500 chars is a bound on a field a THIRD PARTY writes: an app's tool
        # descriptions arrive over the wire and this response renders in the
        # owner's browser. The console clamps visually; this clamps the payload.
        return {"name": name, "description": (description or "")[:500],
                "access": action_access(cid, name),
                "confirm": needs_confirm(cid, name)}

    kind = transport(m)
    error: str | None = None
    source = "declared"
    rows: List[dict] = []

    if kind in ("mcp", "discover"):
        try:
            res = discover_tools(cid) if live else {}
        except Exception as e:  # noqa: BLE001 -- see the docstring; a raise here
            # is a bug in a transport, and the owner's console is not the place
            # to find out about it. It degrades to the cached list like any
            # other failure to reach the app, and says so.
            res = {"error": f"{cid} discovery raised: {type(e).__name__}: {e}"}
        tools = res.get("tools") if isinstance(res, dict) else None
        if isinstance(tools, list) and not res.get("error"):
            source = "live"
            rows = [_row(str(t.get("name") or ""), str(t.get("description") or ""))
                    for t in tools if isinstance(t, dict) and t.get("name")]
        else:
            error = (res.get("error") if isinstance(res, dict) else None) or (
                None if live else "live discovery not attempted (live=0)")
            # The write-through cache the consent gate already reads. Showing the
            # last known list beats showing nothing: the owner asked what this
            # app can do, and "here is what it could do an hour ago, and here is
            # why I cannot ask right now" answers that. `stale` is carried by
            # `source`, so nothing has to infer it from the error being set.
            from . import tools_cache
            cached = tools_cache.for_connector(cid)
            source = "cache" if cached else "none"
            rows = [_row(n, str((v or {}).get("description") or ""))
                    for n, v in sorted(cached.items())]
    elif kind == "rest":
        # Every declared action carrying an id -- INCLUDING those with no
        # `path:`, which a built-in Ava tool serves instead of the generic
        # proxy. `_static_tool_schemas` drops those; they are still things the
        # agent can do, so a surface that omits them under-reports the app.
        rows = [_row(str(a["id"]), str(a.get("description") or ""))
                for a in _static_actions(m) if a.get("id")]
    else:
        source = "none"
        error = "this connector declares no agent surface"

    return {"connector": cid, "label": _display_label(m), "transport": kind,
            "source": source, "tools": rows, "error": error}


def call_discovered(cid: str, name: str, args: dict | None) -> tuple:
    """Invoke one dynamically-discovered tool by name. Returns (data, status)."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    mcp = _mcp_spec(m)
    if mcp:
        from . import mcp_client
        return mcp_client.call_tool(cid, mcp, name, args)
    spec = _discover_spec(m)
    if not spec:
        # A large static connector's <cid>_call: dispatch to the declared action
        # (same generic proxy — and the caller already ran the approvals gate).
        if find_action(cid, name):
            return call_action(cid, name, args)
        return {"error": f"connector {cid} declares no discover spec"}, 400
    base = _discover_base(cid, spec)
    if not base:
        return {"error": f"connector {cid} has no discover base_url"}, 400
    import requests
    try:
        r = requests.post(base + spec["call"],
                          json={"name": name, "arguments": args or {}},
                          headers=_discover_headers(spec), timeout=180,
                          allow_redirects=False)
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {"text": r.text[:4000]}
        return data, r.status_code
    except Exception as e:  # noqa: BLE001
        return unreachable(cid, e, url=base, what="call"), 502


# --- Devices (inbound "app → Ava" channel) ---------------------------------
# A connector opts into pushing events to Ava with an `ingest:` block, and may
# mark itself `role: device` so the Devices view groups it. Both are additive:
# the pull path (actions.discover) is unchanged.
#   ingest:
#     enabled: true
#     channels:                 # OPTIONAL, purely descriptive (nicer Devices UI)
#       - { name: temperature, unit: "°C" }
#       - { name: motion, kind: event }
def ingest_enabled(cid: str) -> bool:
    """True if connector <cid> opted into the inbound event channel."""
    ing = ({x["id"]: x for x in load()}.get(cid) or {}).get("ingest")
    return bool(isinstance(ing, dict) and ing.get("enabled"))


def devices() -> List[dict]:
    """Registry rows for connectors that are devices — either ``role: device`` or an
    ``ingest`` block. Powers ``GET /api/devices`` and ``ava device list``."""
    out = []
    for m in load():
        ing = m.get("ingest") if isinstance(m.get("ingest"), dict) else None
        if m.get("role") != "device" and not ing:
            continue
        out.append({
            "id": m["id"],
            "label": m.get("label", m["id"]),
            "role": m.get("role", "device"),
            "ingest": bool(ing and ing.get("enabled")),
            "channels": [c for c in ((ing or {}).get("channels") or [])
                         if isinstance(c, dict)],
            "discover": bool(_discover_spec(m)),
            "probe": _expand((m.get("service") or {}).get("probe") or "") or None,
        })
    return out


# Consent tiers, on two independent axes that the original three conflated:
# does the call have SIDE EFFECTS, and does it DISCLOSE something?
#
#   read        no side effects, nothing private   -> runs silently
#   sensitive   no side effects, discloses         -> asks once, grantable
#   write       has side effects                   -> asks once, grantable
#   destructive irreversible                       -> asks every time, never grantable
#   physical    moves something real               -> asks every time, never grantable
#
# `sensitive` exists because `read` meant "no side effects" and was implemented as
# "runs silently, forever". An author who wanted "ask before you read my
# conversations" had to label a read `write` or `destructive` — mislabelling it
# either way, and (for `destructive`) training the owner to tap through the prompt
# that actually matters. A silent read from a capability group that can also
# web_fetch an arbitrary URL is an undisclosed disclosure.
#
# Enforcement-identical to `write`; the difference is that the prompt can now say
# truthfully what it is asking about. `approvals.gate` already carries `access` on
# the pending record, so the tier reaches the UI with no plumbing.
_TIERS = ("read", "sensitive", "write", "destructive", "physical")


def _infer_access(a: dict) -> str:
    """The access tier of one action: explicit ``access:`` wins, else inferred —
    GET/HEAD -> read, DELETE or a *delete* id/path -> destructive, else write.
    ``physical`` (moves something in the real world — relay, lock, valve) is
    never inferred: it must be declared, and it can never be granted away."""
    acc = str(a.get("access") or "").lower()
    if acc in _TIERS:
        return acc
    method = str(a.get("method") or "POST").upper()
    if method in ("GET", "HEAD"):
        return "read"
    if method == "DELETE" or "delete" in f"{a.get('id', '')} {a.get('path', '')}".lower():
        return "destructive"
    return "write"


def _dynamic_access(m: dict, action: str) -> str:
    """Tier for a dynamically-discovered tool (MCP / discover facade), which has
    no static entry to infer from. The manifest classifies them by name pattern:

        dynamic_access:
          GetLiveContext: read      # fnmatch patterns, first match wins
          "*": physical

    Unmatched tools fall back to ``physical`` on a ``role: device`` connector
    (a device's unknown verbs are presumed to move something until the author
    says otherwise) and ``write`` everywhere else."""
    import fnmatch
    da = m.get("dynamic_access")
    if isinstance(da, dict):
        for pattern, tier in da.items():
            if not fnmatch.fnmatch(action, str(pattern)):
                continue
            t = str(tier or "").lower()
            if t in _TIERS:
                return t
            # A MATCHED pattern with an unspellable tier. This used to be
            # `if t in _TIERS and fnmatch(...)`, so a typo made the pattern simply
            # not match: `"*publish*": destrucive` fell through to `write`,
            # grantable and silently always-allowable, with no row in
            # load_errors(). An author who writes a tier is asking for a gate, so
            # an unreadable one fails CLOSED to the strictest tier rather than
            # opening the tool up. `_validate` also reports it now, but validation
            # only runs on the load path and this function is reachable from
            # cached manifests, so the guard belongs in both places.
            return "destructive"
    # The app's own ava-tools/1 declaration (write-through cache filled by
    # discover_tools). Manifest patterns above outrank it — the operator's word
    # beats the app's self-report; it outranks the blanket fallback below.
    #
    # Consulted ONLY for a connector Ava has reason to trust. docs/CONNECTOR_SDK.md
    # justifies self-reported tiers with "they can only make a tool *quieter*,
    # never extend its reach" — but when quieter means `read`, and `read` means
    # runs-silently-forever, quiet IS the reach. A server returning
    # `{"name": "wipe_everything", "access": "read"}` was granted silence on its
    # own word, permanently, after one discovery call.
    #
    # That reasoning holds for a loopback app the owner installed and it does not
    # hold for a remote server, which is a case the field predates. So: trust a
    # private/loopback base, or an explicit `trust_declared_tiers: true`, and
    # otherwise ignore the self-report and fall through to the manifest's own
    # default below.
    if _trusts_declared_tiers(m):
        from . import tools_cache
        acc = tools_cache.access(str(m.get("id") or ""), action)
        if acc:
            return acc
    return "physical" if m.get("role") == "device" else "write"


def action_access(cid: str, action: str) -> str:
    """Access tier for <action> on <cid>. Static actions infer from the
    declaration; dynamic tools (MCP / discovered) classify via the manifest's
    ``dynamic_access`` patterns (see _dynamic_access for the defaults)."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    for a in _static_actions(m):
        if a.get("id") == action:
            return _infer_access(a)
    return _dynamic_access(m, action)


def _author_confirm(m: dict, action: str) -> bool:
    """Explicit author gate: connector-level ``confirm: true`` / ``confirm:
    [names]`` or ``confirm: true`` on the static action."""
    c = m.get("confirm")
    if c is True:
        return True
    if isinstance(c, list) and action in c:
        return True
    for a in _static_actions(m):
        if a.get("id") == action and a.get("confirm"):
            return True
    return False


def needs_confirm(cid: str, action: str) -> bool:
    """True if <action> on connector <cid> requires operator approval before it
    runs (JIT consent — docs/dev/CONNECTOR_DISCOVERY_UX_PLAN.md):

    - an explicit author ``confirm:`` always asks and can't be granted away;
    - ``read`` actions run silently;
    - ``sensitive`` actions ask on first use — no side effects, but they
      DISCLOSE something (a chat corpus, a mailbox, a location history);
    - ``destructive`` actions ask every time;
    - ``physical`` actions (real-world actuation) ask every time — never
      grantable, so a lock can't become a one-tap-then-silent action;
    - ``write`` actions ask on first use — an "Always allow" grant
      ($AVA_HOME/connector_grants.yaml) silences later calls."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    if _author_confirm(m, action):
        return True
    tier = action_access(cid, action)
    if tier == "read":
        return False
    if tier in ("destructive", "physical"):
        return True
    from . import grants
    return not grants.has(cid, action)


def grantable(cid: str, action: str) -> bool:
    """True when the approval prompt may offer "Always allow": ``write`` and
    ``sensitive`` (read never asks; destructive and physical are never grantable;
    author confirm sticks)."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    return (not _author_confirm(m, action)
            and action_access(cid, action) in ("write", "sensitive"))


def action_capability(a: dict) -> str:
    """The UI grouping for one action: explicit ``capability:`` wins, else the
    first meaningful path segment (api/v1 prefixes skipped) — /api/persona/{key}
    groups under "persona"."""
    cap = str(a.get("capability") or "").strip().lower()
    if cap:
        return cap
    segs = [s for s in str(a.get("path") or "").split("/") if s and not s.startswith("{")]
    if len(segs) > 1 and segs[0] in ("api", "v1", "v2", "rest"):
        segs = segs[1:]
    return segs[0] if segs else "general"


def all() -> List[dict]:
    return load()
