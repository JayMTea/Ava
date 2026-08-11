"""What the NemoClaw sandbox is actually running, versus what this repo declares.

Ava's Setup UI writes persona, skills, policies and tool-server config to disk;
none of it reaches the agent until `agent/install.sh` pushes it into the sandbox.
Until this module existed, only skills had any notion of that gap, and only as a
*promise*: `install.sh` wrote a manifest of what it had tried to install, and the
Agent tab believed it.

A promise file is only ever as honest as its last writer, and it had already
failed three ways on the box this was written on — the manifest did not exist at
all (so eight skills read `unknown` while a persona, seven policies and five MCP
servers were demonstrably live); a failed `skill install` still got a manifest
row, because install.sh swallowed the error; and a hand-run of `cd agent &&
./install.sh` writes only the skills row, so any persona/policy row would go
stale the moment somebody provisioned from the Hub instead.

So drift here is **derived, never stored**:

  * **Policies** come from `~/.nemoclaw/sandboxes.json → customPolicies[]`, which
    carries the full applied content and is authoritative. One 40 KB file read
    answers every policy, plus the versions and the image tag — and it works with
    the container stopped, which is the state a box is in exactly when you most
    want to know what is live. `sha256(entry.content)` and `sha256(file bytes)`
    were verified byte-identical for all seven applied policies.
  * **Skills** fall back to install.sh's manifest, because there is no
    `nemoclaw skill list` to ask. A live digest probe supersedes it when the
    sandbox is reachable.
  * **Persona and servers** need a live probe. Until one runs they are `unknown`,
    which is the honest answer and the reason the ladder has four states rather
    than three.

`data/provision_run.json` records **provenance only** — when, what scope, what
exit code, and the `imageTag` baseline for rebuild detection. Nothing in the
drift path reads it for truth, so a stale record from a hand-run is harmless.

Kept out of `skills.py` and `policy_inventory.py` on purpose. `policy_inventory`
returns facts and "deliberately does not decide"; drift is a decision. And
teaching `skills.py` about the sandbox would make `hub/agent.py` import a skill
catalog to learn about egress policy. The dependency runs one way:
`provision → skills`, never back.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import threading
import time
from typing import Any, Callable

from . import config, policy_inventory, settings, skills

# The four states, and the whole vocabulary the UI is allowed to speak. They are
# the ones `skills.py` already used, so the Agent tab's badges keep working.
STATES = ("deployed", "stale", "undeployed", "unknown")

# Scope names are shared with `agent/install.sh --only` and the UI's domain keys.
# One spelling everywhere; the UI is free to *label* `servers` as "Tools".
SCOPES = ("persona", "policies", "servers", "skills")
ALL_SCOPES = (*SCOPES, "all")

# Worst-wins severity when rolling item states up to a scope. `stale` outranks
# `undeployed` because it is the only state where the sandbox is actively running
# content a human believes was replaced: a missing item fails loudly, a stale one
# lies quietly.
_SEVERITY = {"deployed": 0, "unknown": 1, "undeployed": 2, "stale": 3}

# Mirrors install.sh's NAME_OVERRIDE. A private overlay may pin more names via
# overlay/agent/server-names.sh, which is shell and is not parsed here — an
# overlay server therefore verifies by path rather than by name.
_SERVER_NAME_OVERRIDE = {"admin": "ava-admin"}

_CACHE: dict[str, Any] = {"ts": 0.0, "state": None}
_LOCK = threading.Lock()
_render_persona: Callable[[], str] | None = None


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def run_record_path() -> str:
    """Where a provisioning run records what it attempted. Provenance, not truth."""
    return os.path.join(settings.data_dir(), "provision_run.json")


def _server_dirs() -> dict[str, str]:
    """{category: abs dir} for every mcp_server_<category>/ with a _server.mjs.

    Same discovery rule as install.sh §2c, including the overlay.
    """
    out: dict[str, str] = {}
    overlay = os.environ.get("AVA_OVERLAY") or os.path.join(config.ROOT, "overlay", "agent")
    for base in (os.path.join(config.ROOT, "agent"), overlay):
        if not os.path.isdir(base):
            continue
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for fn in entries:
            if not fn.startswith("mcp_server_"):
                continue
            d = os.path.join(base, fn)
            if os.path.isfile(os.path.join(d, "_server.mjs")):
                out[fn[len("mcp_server_"):]] = d
    return out


def server_name(category: str) -> str:
    return _SERVER_NAME_OVERRIDE.get(category, f"ava-tools-{category}")


def server_root(category: str) -> str:
    """The sandbox directory install.sh extracts this server's tarball into."""
    return f"/sandbox/.openclaw/mcp_server_{category}"


def server_path(category: str) -> str:
    return f"{server_root(category)}/_server.mjs"


# --------------------------------------------------------------------------- #
# Digests — the producer/consumer contract. Get the bytes wrong and drift lies.
# --------------------------------------------------------------------------- #
def _persona_render() -> str:
    """The rendered system prompt, exactly as install.sh would send it.

    Loaded by path rather than imported: `agent/render_persona.py` does a
    module-level `sys.path.insert(0, REPO)`, and importing `ava_bridge.provision`
    must not have the side effect of making `agent/` importable package-wide.
    """
    global _render_persona
    if _render_persona is None:
        path = os.path.join(config.ROOT, "agent", "render_persona.py")
        spec = importlib.util.spec_from_file_location("ava_render_persona", path)
        if spec is None or spec.loader is None:
            raise FileNotFoundError(path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _render_persona = mod.render
    return _render_persona()


def persona_digest() -> str:
    """sha256 of the rendered persona.

    NO trailing newline: install.sh pipes it with `printf %s`, so a newline here
    would put every install permanently one byte out from the sandbox and report
    a persona that is actually current as `stale`, forever.
    """
    return hashlib.sha256(_persona_render().encode("utf-8")).hexdigest()


def _bridge_port() -> int:
    try:
        return int(config.SERVER_PORT)
    except (TypeError, ValueError):
        return 8096


def port_rewrite(raw: bytes) -> bytes:
    """Apply install.sh's policy port substitution to a policy's bytes.

    `install.sh` rewrites `port: 8096` onto a temp copy when the bridge has moved,
    so on such a box the applied content differs from the tracked file. Comparing
    the tracked bytes would report EVERY policy stale forever. `\\b` matches sed's
    word boundary, so `port: 80960` is left alone the same way.
    """
    port = _bridge_port()
    if port == 8096:
        return raw
    return re.sub(rb"port: 8096\b", f"port: {port}".encode(), raw)


def _file_digest(path: str, transform: Callable[[bytes], bytes] | None = None) -> str:
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    if transform is not None:
        raw = transform(raw)
    return hashlib.sha256(raw).hexdigest()


def _tree_map(root: str) -> dict[str, str]:
    """{relpath: sha256} for every file under `root`."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            out[os.path.relpath(full, root)] = _file_digest(full)
    return out


def _fold(rows: dict[str, str]) -> str:
    return hashlib.sha256(
        "\n".join(sorted(f"{rel}\0{sha}" for rel, sha in rows.items()))
        .encode("utf-8")).hexdigest()


def tree_digest(root: str) -> str:
    """A stable fold over a server directory: sorted `relpath\\0sha256` lines.

    NOT a digest of the tarball install.sh ships — `tar czf` embeds mtimes and a
    gzip header, so that would change on every checkout. install.sh does
    `rm -rf "$DEST"` before extracting, so the sandbox copy is a byte-exact
    mirror of this tree and the fold is comparable on both sides — which is what
    `AgentRuntime.tree_digests()` reproduces from inside the sandbox.
    """
    return _fold(_tree_map(root))


def merged_tree_digest(roots: list[str]) -> str:
    """`tree_digest` over several roots overlaid in order, later winning.

    The repo-side twin of install.sh's stage-then-tar: it copies the shipped
    server directory and then the generated one over the top, into a single
    tarball. Fold anything else here and every server reads `stale` forever.
    """
    rows: dict[str, str] = {}
    for root in roots:
        rows.update(_tree_map(root))
    return _fold(rows)


def server_sources(category: str, primary: str | None = None) -> list[str]:
    """Every directory install.sh stages into this server's tarball, in copy
    order (later wins on a filename collision).

    A server is discovered by having a `_server.mjs` (§2c), which generated
    material does not — so the connector tools under the agent state root are a
    MERGE onto the shipped server, never a second discovered root that could
    shadow it. Public because drift has to fold exactly what ships.
    """
    if primary is None:
        primary = _server_dirs().get(category, "")
    if not primary:
        return []
    gen = os.path.join(settings.agent_state_dir(), f"mcp_server_{category}")
    if os.path.isdir(gen) and os.path.realpath(gen) != os.path.realpath(primary):
        return [primary, gen]
    return [primary]


# --------------------------------------------------------------------------- #
# Desired (repo side) — no CLI, no sandbox, never raises
# --------------------------------------------------------------------------- #
def desired() -> dict[str, list[dict]]:
    """What this checkout says the sandbox should be running."""
    out: dict[str, list[dict]] = {"persona": [], "policies": [], "servers": [], "skills": []}

    try:
        out["persona"] = [{
            "id": "IDENTITY.md",
            "label": "Persona",
            "sha256": persona_digest(),
            "rel": "agent/persona.txt.tmpl",
        }]
    except Exception as e:  # noqa: BLE001 — a broken template must not 500 the panel
        out["persona"] = [{
            "id": "IDENTITY.md", "label": "Persona", "sha256": "",
            "rel": "agent/persona.txt.tmpl", "error": f"could not render: {e}",
        }]

    for p in policy_inventory.inventory():
        out["policies"].append({
            "id": p.name,                      # preset.name — NOT the file stem
            "label": p.name,
            "sha256": _file_digest(p.path, port_rewrite),
            "rel": p.rel,
            "source": p.source,
            "parse_error": p.parse_error,
        })

    for cat, src in sorted(_server_dirs().items()):
        out["servers"].append({
            "id": server_name(cat),
            "label": server_name(cat),
            "sha256": merged_tree_digest(server_sources(cat, src)),
            "rel": os.path.relpath(src, config.ROOT),
            "category": cat,
            "path": server_path(cat),
            # The directory the tarball lands in. `path` identifies the server
            # to openclaw.json; `sandbox_root` is what gets folded against
            # `sha256`, and the two must stay distinct — comparing a tree fold
            # against an entry-point digest is what broke drift before.
            "sandbox_root": server_root(cat),
        })

    for s in skills.catalog():
        out["skills"].append({
            "id": s["dir"],                    # keyed on DIRECTORY, like the manifest
            "label": s.get("title") or s["dir"],
            "sha256": s.get("sha256") or "",
            "rel": os.path.join("agent", "skills", s["dir"], "SKILL.md"),
            # The sandbox installs a skill under its frontmatter `name:`, which is
            # not always the directory name. Carried so a live probe can find it.
            "sandbox_id": s.get("id") or s["dir"],
        })

    return out


# --------------------------------------------------------------------------- #
# Observed (sandbox side) — cheap sources only; never execs
# --------------------------------------------------------------------------- #
def _manifest_map() -> dict[str, str] | None:
    """{skill dir: sha256} from install.sh's manifest, or None if never written."""
    try:
        with open(skills.manifest_path(), encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    return {str(r["name"]): str(r.get("sha256") or "")
            for r in rows if isinstance(r, dict) and r.get("name")}


def observed(rt=None, live_probe: bool = True) -> dict[str, Any]:
    """What we can see without touching the sandbox.

    Returns per-scope `{id: sha256}` maps plus the `source` that produced each
    scope — and `source` is the whole point, because it is what separates "we
    looked and it is not there" from "we could not look".
    """
    from . import runtime as _runtime
    rt = rt or _runtime.configured()

    record = None
    try:
        record = rt.registry_record()
    except Exception:  # noqa: BLE001 — a missing/renamed registry is not an error
        record = None

    out: dict[str, Any] = {
        "record": record,
        "maps": {s: None for s in SCOPES},
        "sources": {s: "none" for s in SCOPES},
    }

    if record:
        applied: dict[str, str] = {}
        for entry in (record.get("customPolicies") or []):
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            content = entry.get("content")
            applied[str(entry["name"])] = hashlib.sha256(
                str(content or "").encode("utf-8")).hexdigest()
        out["maps"]["policies"] = applied
        out["sources"]["policies"] = "registry"

    manifest = _manifest_map()
    if manifest is not None:
        out["maps"]["skills"] = manifest
        out["sources"]["skills"] = "manifest"

    # Persona, servers and (better than the manifest) skills need a live probe.
    # Gated on the sandbox actually running: a probe that could not run must yield
    # `unknown`, never an empty result read as "nothing is installed". That
    # mistake would show the owner a to-do list of things that are in fact live.
    if live_probe:
        try:
            if rt.live().get("live"):
                out.update(_probe(rt, out))
        except Exception:  # noqa: BLE001 — a probe failure is `unknown`, not an error
            pass
    return out


PERSONA_PATH = "/sandbox/.openclaw/workspace/IDENTITY.md"
SKILLS_GLOB = "/sandbox/.openclaw/skills/*/SKILL.md"


def _probe(rt, out: dict) -> dict:
    """Live digests from inside the sandbox. Four execs total, not one per item."""
    maps = dict(out["maps"])
    sources = dict(out["sources"])

    # One exec covers the persona plus every server entry point. The entry
    # points are no longer *compared* — `_registered_servers` folds whole trees
    # now — but they stay in this call as the witness that the exec channel
    # works. Without one, an empty result could not be told apart from "the
    # persona is absent", and absence has to stay reportable as `undeployed`.
    want_servers = {row["id"]: row for row in desired()["servers"]}
    paths = [PERSONA_PATH] + [r["path"] for r in want_servers.values()]
    got = rt.digest(paths)
    if got:
        # `got` already carries the persona digest — re-execing for it was one
        # round trip spent learning what we had just been told.
        maps["persona"] = ({"IDENTITY.md": got[PERSONA_PATH]}
                           if PERSONA_PATH in got else {})
        sources["persona"] = "probe"

    # Servers: registration is the question openclaw.json answers, and `mcp list
    # --json` cannot — it reports only `nemoclaw mcp add` HTTP bridges and comes
    # back with `bridges: []` while five servers are registered.
    registered = _registered_servers(rt, want_servers)
    if registered is not None:
        maps["servers"] = registered
        sources["servers"] = "probe"

    skills_live = _skill_digests(rt)
    if skills_live is not None:
        maps["skills"] = skills_live
        sources["skills"] = "probe"

    return {"maps": maps, "sources": sources}


def _extra(have: dict[str, str] | None, want_ids: set[str],
           source: str) -> list[str] | None:
    """What the sandbox holds that this checkout does not declare.

    `None` when there was no evidence source: "we could not look" must not read
    as "there is nothing extra", for the same reason it must not read as
    `undeployed` on the way in. Every comparison in this module walked `want` and
    looked each item up in `have`; nothing walked the other way, so anything left
    behind in the sandbox was structurally invisible.
    """
    if have is None or source == "none":
        return None
    return sorted(set(have) - want_ids)


def _registered_servers(rt, want: dict[str, dict] | None = None) -> dict[str, str] | None:
    """{server name: tree fold} for servers registered in openclaw.json.

    Two questions, deliberately answered by two different sources:

      * **Presence** is what openclaw.json answers. A server not registered, or
        registered pointing at some other path, is absent — it never reaches the
        digest comparison, so it reads `undeployed`.
      * **Content** is what the tree fold answers, against the SAME fold
        `desired()` computes over the repo directory (`tree_digest`). install.sh
        does `rm -rf "$DEST"` before extracting `tar czf - -C "$src" .`, so the
        two sides are byte-comparable.

    This used to report the sandbox's ENTRY-POINT digest against the repo's
    WHOLE-TREE digest. Those are folds of different byte sets and can never be
    equal, so every registered server read `stale` in perpetuity: the pending
    count never reached zero, and `provision_job`'s post-apply assert vetoed
    every run that had in fact succeeded.

    `None` (never `{}`) when we could not look — see `AgentRuntime.tree_digests`.
    """
    raw = rt.read_file("/sandbox/.openclaw/openclaw.json")
    if not raw:
        return None
    try:
        cfg = json.loads(raw[raw.find("{"):])
    except ValueError:
        return None
    servers = ((cfg.get("mcp") or {}).get("servers") or {})
    if not isinstance(servers, dict):
        return None
    if want is None:
        want = {row["id"]: row for row in desired()["servers"]}

    # Only fold the servers that are actually registered at the expected path —
    # an unregistered one is absent regardless of what is on disk, and folding
    # it would pay for an answer we then throw away.
    live_names = {
        name: row for name, row in want.items()
        if isinstance(servers.get(name), dict)
        and (servers[name].get("args") or [None])[0] == row["path"]
    }
    # Registered under an `ava-` name this checkout no longer declares: an
    # orphan. Carried with an empty digest because only its NAME is wanted —
    # `item_state` only ever looks up ids from `want`, so an extra key here can
    # never affect drift, and without it the orphan is invisible to `_extra`.
    extras = {name: "" for name in servers
              if name.startswith("ava-") and name not in want}
    if not live_names:
        return extras
    folds = rt.tree_digests([row["sandbox_root"] for row in live_names.values()])
    if folds is None:
        return None  # no evidence source -> `unknown`, not a to-do list
    return {**extras,
            **{name: folds[row["sandbox_root"]]
               for name, row in live_names.items()
               if row["sandbox_root"] in folds}}


def _skill_digests(rt) -> dict[str, str] | None:
    """{repo dir: sha256} for skills present in the sandbox.

    There is no `nemoclaw skill list`, so this globs. Skills land under their
    SKILL.md frontmatter `name:`, which is not always the repo directory name —
    the result is re-keyed onto the directory so it lines up with the manifest
    and with skills.catalog().
    """
    if not hasattr(rt, "glob_digest"):
        return None
    live = rt.glob_digest(SKILLS_GLOB)
    if not live:
        return None
    by_sandbox_id = {}
    for path, sha in live.items():
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            by_sandbox_id[parts[-2]] = sha
    out: dict[str, str] = {}
    for row in desired()["skills"]:
        sid = row.get("sandbox_id") or row["id"]
        if sid in by_sandbox_id:
            out[row["id"]] = by_sandbox_id[sid]
        elif row["id"] in by_sandbox_id:
            out[row["id"]] = by_sandbox_id[row["id"]]
    return out


# --------------------------------------------------------------------------- #
# The ladder
# --------------------------------------------------------------------------- #
def item_state(want: str, have: str | None, *, source: str, rebuilt: bool = False) -> str:
    """deployed | stale | undeployed | unknown.

    `source` is where `have` came from, and it is load-bearing:
        registry  — ~/.nemoclaw/sandboxes.json (authoritative, works offline)
        probe     — a live sha256sum inside the sandbox (authoritative)
        manifest  — install.sh's record; authoritative for ABSENCE only, because
                    install.sh writes a row per skill it attempted
        none      — nothing could observe this item at all
    """
    if rebuilt:
        # The sandbox was rebuilt out from under us, so its contents are provably
        # gone. That is positive evidence of absence, available with the container
        # stopped — stronger and cheaper than any version comparison.
        return "undeployed"
    if source == "none":
        return "unknown"
    if have is None:
        return "undeployed"
    if have != want:
        return "stale"
    return "deployed"


def _roll_up(states: list[str]) -> str:
    if not states:
        return "deployed"
    return max(states, key=lambda s: _SEVERITY.get(s, 0))


# --------------------------------------------------------------------------- #
# The merged payload
# --------------------------------------------------------------------------- #
def state(rt=None, force: bool = False) -> dict:
    """Everything the Hub needs to answer "is what I saved actually live?"."""
    with _LOCK:
        cached = _CACHE.get("state")
        if not force and cached is not None and time.time() - float(_CACHE["ts"]) < 30:
            return cached  # type: ignore[return-value]

    from . import runtime as _runtime
    rt = rt or _runtime.configured()

    want = desired()
    obs = observed(rt)
    record = obs["record"] or {}
    run = load_run()

    image_tag = record.get("imageTag") or None
    rebuilt = bool(image_tag and run and run.get("image_tag")
                   and image_tag != run.get("image_tag"))

    items: list[dict] = []
    scopes: dict[str, dict] = {}
    for scope in SCOPES:
        source = obs["sources"][scope]
        have = obs["maps"][scope]
        counts = dict.fromkeys(STATES, 0)
        # A rebuild only invalidates what lives INSIDE the sandbox image. Policies
        # are gateway rules recorded host-side, so they survive it.
        scope_rebuilt = rebuilt and scope != "policies"
        for row in want[scope]:
            st = item_state(
                row["sha256"],
                None if have is None else have.get(row["id"]),
                source=source,
                rebuilt=scope_rebuilt,
            )
            counts[st] += 1
            items.append({**row, "scope": scope, "state": st, "source": source,
                          "observed_sha256": (None if have is None
                                              else have.get(row["id"]))})
        counts["total"] = len(want[scope])  # type: ignore[assignment]
        scopes[scope] = {
            "state": _roll_up([i["state"] for i in items if i["scope"] == scope]),
            "counts": counts,
            # `unknown` is NEVER pending. It means we could not look, not that
            # something is missing — counting it inflates the number and makes
            # Apply look like it did less than it did.
            "pending": counts["stale"] + counts["undeployed"],
            "source": source,
        }

    live, live_reason = _liveness(rt)
    total = dict.fromkeys(STATES, 0)
    for i in items:
        total[i["state"]] += 1
    total["total"] = len(items)  # type: ignore[assignment]

    payload = {
        "ok": True,
        "state": _roll_up([i["state"] for i in items]),
        "checked_at": time.time(),
        "runtime": config.AGENT_RUNTIME,
        "enabled": config.AGENT_ENABLED,
        "location": "local" if rt is _runtime.nemoclaw() else "remote",
        "sandbox": {
            "name": getattr(rt, "sandbox", None),
            "live": live,
            "reason": live_reason,
            "image_tag": image_tag,
            "rebuilt": rebuilt,
            "model": record.get("model"),
            "provider": record.get("provider"),
            "gateway_port_recorded": record.get("gatewayPort"),
            "versions": {
                "nemoclaw": record.get("nemoclawVersion"),
                "openshell": record.get("openshellVersion"),
                "agent": record.get("agentVersion"),
            },
        },
        "run": run,
        # What the sandbox holds that this checkout does not declare. Additive
        # and separate from the four-state ladder ON PURPOSE: `deployed | stale |
        # undeployed | unknown` is a published vocabulary the UI derives from,
        # and an orphan is not a fifth kind of drift — it is not something Apply
        # fixes. `policy-add` has no remove verb, so a deleted connector's egress
        # allowance lives in the sandbox until a rebuild; reporting it is the
        # honest thing we CAN do, and saying nothing is what let it accumulate.
        # A `None` for a scope means we could not look, never "nothing extra".
        "orphans": {
            scope: _extra(obs["maps"][scope],
                          {row["id"] for row in want[scope]},
                          obs["sources"][scope])
            for scope in SCOPES if scope != "persona"
        },
        "scopes": scopes,
        "items": items,
        "counts": total,
        "pending": sum(s["pending"] for s in scopes.values()),
        "scopes_to_provision": [s for s in SCOPES if scopes[s]["pending"]],
    }

    with _LOCK:
        _CACHE.update(ts=time.time(), state=payload)
    return payload


def _liveness(rt) -> tuple[bool, str]:
    try:
        info = rt.live()
    except Exception as e:  # noqa: BLE001
        return False, f"could not probe the runtime: {e}"
    return bool(info.get("live")), str(info.get("reason") or "")


def summary(rt=None, force: bool = False) -> dict:
    """The rollup the pending-changes bar consumes."""
    st = state(rt, force=force)
    return {
        "pending": st["pending"],
        "state": st["state"],
        "scopes": {k: {"pending": v["pending"], "state": v["state"]}
                   for k, v in st["scopes"].items()},
        "sandbox_live": st["sandbox"]["live"],
        "scopes_to_provision": st["scopes_to_provision"],
    }


def apply_skill_states(catalog: list[dict], rt=None) -> list[dict]:
    """Return a COPY of `skills.catalog()` with each row's `deployed` replaced by
    the provision-derived state.

    A pure overlay so `skills.py` never learns about the sandbox and the frontend's
    `Skill.deployed` contract stays byte-identical.
    """
    try:
        by_id = {i["id"]: i["state"] for i in state(rt)["items"] if i["scope"] == "skills"}
    except Exception:  # noqa: BLE001 — never let drift break the skills list
        return catalog
    return [{**s, "deployed": by_id.get(s["dir"], s.get("deployed", "unknown"))}
            for s in catalog]


def verify(rt=None, scope: str = "all", live_probe: bool = True) -> dict:
    """Did what we just deployed actually land?

    The honesty rule is the liveness gate. Registry-sourced checks (policies,
    rebuild detection) always run — they work with the container stopped, which
    is the whole reason drift reads the registry. Probe-sourced checks return
    `checked: false` and their items get `verified: None` — never False, never
    True. "We could not look" is not a verdict, and recording it as one is how a
    report starts lying at exactly the moment it matters.

    Returns {checked, live, reason, at, items, failed, scopes}.
    """
    from . import runtime as _runtime
    rt = rt or _runtime.configured()
    invalidate()
    st = state(rt=rt, force=True)

    live = bool(st["sandbox"]["live"])
    wanted = SCOPES if scope in ("all", None) else tuple(
        s for s in scope.split(",") if s in SCOPES)

    items: list[dict] = []
    failed: list[str] = []
    scopes_checked: dict[str, bool] = {}
    for row in st["items"]:
        if row["scope"] not in wanted:
            continue
        source = row["source"]
        # `manifest` is install.sh's record of intent, not evidence of outcome —
        # it writes a row even for a skill whose install failed. It can prove
        # absence, never success, so it never yields `verified: True`.
        checkable = source in ("registry", "probe")
        scopes_checked[row["scope"]] = scopes_checked.get(row["scope"], False) or checkable
        if not checkable:
            verified = None
            detail = ("the sandbox is not running, so this could not be checked"
                      if not live else "no evidence source for this item")
        elif row["state"] == "deployed":
            verified, detail = True, ""
        else:
            verified = False
            detail = {
                "undeployed": "not present in the sandbox",
                "stale": "present but the content differs from this checkout",
                "unknown": "could not be determined",
            }.get(row["state"], row["state"])
            failed.append(f"{row['scope']}/{row['id']}")
        items.append({**row, "verified": verified, "verify_detail": detail})

    return {
        "checked": any(scopes_checked.values()),
        "live": live,
        "reason": st["sandbox"]["reason"],
        "at": time.time(),
        "scope": scope,
        "scopes": scopes_checked,
        "items": items,
        "failed": failed,
    }


def verify_step(result: dict) -> dict | None:
    """Fold a verify() result into one `{step, ok, detail}` row.

    Both forms are deliberate. The per-item `verified` flag is what the UI paints
    — a row reading "applied · content differs" is actionable in a way a counter
    is not. This rolled-up step is what every EXISTING surface already renders,
    including `ava_cli.py`'s step loop, with no changes to either.
    """
    if not result or not result.get("checked"):
        return None
    failed = result.get("failed") or []
    if not failed:
        return {"step": "verify", "ok": True,
                "detail": f"{len(result.get('items') or [])} item(s) confirmed in the sandbox"}
    shown = ", ".join(failed[:3]) + (f" (+{len(failed) - 3} more)" if len(failed) > 3 else "")
    return {"step": "verify", "ok": False,
            "detail": f"{len(failed)} item(s) did not land: {shown}"}


def invalidate() -> None:
    with _LOCK:
        _CACHE.update(ts=0.0, state=None)
    skills.invalidate()


# --------------------------------------------------------------------------- #
# Provenance record
# --------------------------------------------------------------------------- #
def load_run() -> dict | None:
    try:
        with open(run_record_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def record_run(*, scope: str, source: str, started: float, ended: float,
               rc: int, steps: list[dict] | None = None,
               verify: dict | None = None, rt=None) -> dict:
    """Write `data/provision_run.json` atomically. Provenance only.

    Deliberately NOT a drift source: a hand-run of `cd agent && ./install.sh`
    leaves this stale, and nothing here would notice or care.
    """
    from . import runtime as _runtime
    from . import version as _version
    rt = rt or _runtime.configured()
    try:
        record = rt.registry_record() or {}
    except Exception:  # noqa: BLE001
        record = {}

    payload = {
        "schema": 1,
        "source": source,
        "scope": scope,
        "started": started,
        "ended": ended,
        "rc": rc,
        "sandbox": getattr(rt, "sandbox", None),
        "image_tag": record.get("imageTag"),
        "versions": {
            "ava": _version.version(),
            "nemoclaw": record.get("nemoclawVersion"),
            "openshell": record.get("openshellVersion"),
            "agent": record.get("agentVersion"),
            "model": record.get("model"),
        },
        "steps": steps or [],
        "verify": verify,
    }
    path = run_record_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        pass  # provenance is best-effort; drift does not depend on it
    invalidate()
    return payload
