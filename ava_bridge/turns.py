"""Live "chain of thought" turns.

The `openclaw agent` CLI is non-streaming (one JSON blob at the end) and the
Nemotron model only supports thinking=off, so we can't stream reasoning tokens.
BUT the agent persists every step to its session jsonl as it works — including
{type:"thinking"} reasoning, intermediate {type:"text"} and {type:"toolCall"}
events. We run the (blocking) turn in a worker thread and concurrently poll the
tail of that session file, so the UI can show Ava's REAL reasoning + actions
live, then attach the final reply / image when she finishes.
"""
import json
import shlex
import threading
import time
import uuid

import requests

from . import audit, config, connectors, state, runtime
from .agent import (ask_openclaw, which_model, sbx_read, session_file,
                    chat_direct)
from .artifacts import build_turn_artifact
from .chat_store import chat_append, history_for
from .gpu_jobs import (pickup_image_since, start_agent_image_watch, attach_chat,
                         _latest_image_job_since)


def _pickup_previews_since(t0: float, tools: list[str]) -> list[dict]:
    """Deterministic action data for chat quick-buttons.

    Connectors can declare a ``chat_pickup:`` block (see connectors.chat_pickups):
    when a turn used one of the named tools, read the app's append-only render
    log for artifacts produced since the turn started and return one card dict
    per artifact. No LLM / trajectory parsing — the app is the single source of
    truth, so the UI can offer deterministic quick-buttons tied to real images.
    App-relative URLs are rewritten through the same-origin /apps/<id> proxy.
    """
    out: list[dict] = []
    for spec in connectors.chat_pickups():
        if not any(t in tools for t in spec["tools"]):
            continue
        try:
            r = requests.get(spec["url"], params=spec["params"], timeout=8)
            rows = (r.json() or {}).get(spec["list_key"], [])
        except Exception:  # noqa: BLE001 — cards are best-effort
            continue
        for rec in rows:  # app logs return newest-first
            if float(rec.get(spec["ts_key"]) or 0) < t0 - 2:
                continue
            item = {k: rec.get(src) for k, src in spec["fields"].items()}
            url = item.get("url")
            if not url:
                continue
            if url.startswith("/") and spec["url_prefix"]:
                item["url"] = spec["url_prefix"] + url
            out.append(item)
    return out


def _tools_from_session(sid: str, after: int) -> list[str]:
    """Authoritative tool list for a finished turn, read from the session trajectory.

    `openclaw agent --json` does not reliably surface a `toolSummary`, so tool
    usage came back empty even when a tool clearly ran — which broke the tool
    chips AND the artifact side-panel (weather etc.), since both key off this
    list. The session jsonl DOES record every `toolCall` with its name, so we
    parse that (same source the live chain-of-thought already reads) as the
    source of truth."""
    path = session_file(sid)
    if not path:
        return []
    cmd = f"tail -n +{after} {shlex.quote(path)} 2>/dev/null | head -c 400000"
    try:
        steps = _parse_turn_steps(sbx_read(cmd))
    except Exception:  # noqa: BLE001
        return []
    seen: list[str] = []
    for s in steps:
        if s.get("kind") == "tool":
            name = s.get("name")
            if name and name not in seen:
                seen.append(name)
    return seen


def _session_line_count(sid: str) -> int:
    path = session_file(sid)
    try:
        out = sbx_read(f"wc -l < {shlex.quote(path)} 2>/dev/null || echo 0")
        return int((out.strip() or "0").split()[0])
    except Exception:  # noqa: BLE001
        return 0


def _parse_turn_steps(text: str) -> list[dict]:
    """Turn appended session-jsonl lines into an ordered list of CoT steps."""
    steps: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:  # noqa: BLE001 — last line may be mid-write
            continue
        if o.get("type") != "message":
            continue
        msg = o.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "thinking":
                tx = (b.get("thinking") or "").strip()
                if tx:
                    steps.append({"kind": "thinking", "text": tx})
            elif bt == "text":
                tx = (b.get("text") or "").strip()
                if tx:
                    steps.append({"kind": "text", "text": tx})
            elif bt == "toolCall":
                steps.append({"kind": "tool", "name": b.get("name") or "tool"})
    return steps


def _read_session_steps(sid: str, after: int) -> list[dict]:
    """Read the complete CoT trajectory for a finished turn (same source as the
    live poller). Used to persist durable chain-of-thought with the chat message."""
    try:
        path = session_file(sid)
        if not path:
            return []
        cmd = f"tail -n +{after} {shlex.quote(path)} 2>/dev/null | head -c 400000"
        return _parse_turn_steps(sbx_read(cmd))
    except Exception:  # noqa: BLE001
        return []


def _poll_turn_steps(tid: str, sid: str, after: int):
    path = session_file(sid)
    cmd = f"tail -n +{after} {shlex.quote(path)} 2>/dev/null | head -c 400000"
    while True:
        with state.turns_lock:
            running = state.turns.get(tid, {}).get("status") == "running"
        if not running:
            break
        try:
            steps = _parse_turn_steps(sbx_read(cmd))
            with state.turns_lock:
                if tid in state.turns and steps:
                    state.turns[tid]["steps"] = steps
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.1)
    # One last read so the final reasoning step isn't missed.
    try:
        steps = _parse_turn_steps(sbx_read(cmd))
        with state.turns_lock:
            if tid in state.turns and steps:
                state.turns[tid]["steps"] = steps
    except Exception:  # noqa: BLE001
        pass


def _set_turn(tid: str, **fields) -> None:
    """Update a turn's record if it still exists, under the lock.

    Sibling paths in this module disagreed about whether the turn can still be
    there: two guarded with `if tid in state.turns` and four assumed it. The
    assumption is wrong because _prune_turns can evict a turn while it is still
    running (see below), and a KeyError raised there escapes into the worker
    thread, so the turn never reaches a terminal status and the poller waits
    forever on a record that no longer exists. One helper, one check.
    """
    with state.turns_lock:
        if tid in state.turns:
            state.turns[tid].update(**fields)


def _prune_turns(max_age: float = 3600.0):
    now = time.time()
    with state.turns_lock:
        # Never evict a turn that is still running. Age alone was the whole test,
        # and this runs at the top of every start_turn — so a turn that legitimately
        # outlives max_age (a long agent turn, OC_TIMEOUT is up to 600s, or a
        # queued image render) could be deleted mid-flight by an unrelated new
        # message. Its completion then hit a missing key and the client polled a
        # turn id that had ceased to exist.
        stale = [k for k, v in state.turns.items()
                 if now - v.get("created", now) > max_age
                 and v.get("status") != "running"]
        for k in stale:
            state.turns.pop(k, None)


# HARD INVARIANT (owner instruction, 2026-07-15): Ava must NEVER have access to
# passwords and must NEVER reveal one. "I don't have access" is the CORRECT
# answer, not a failure — we only upgrade the bare shrug into a useful pointer
# ("reset it in <app>"). This note rides EVERY turn; the wording is guarded by
# tests/test_tooling_note.py so a refactor can't silently weaken it.
def _credentials_note() -> str:
    from . import connectors
    apps = []
    try:
        apps = [a.get("label") or a.get("id") for a in connectors.apps()]
    except Exception:  # noqa: BLE001
        apps = []
    where = (" Your connected apps are: " + ", ".join(a for a in apps if a) + "."
             if apps else "")
    return (f"[note for {config.AVA_NAME} — not from the user: you do NOT store, retrieve, or "
            "have access to any passwords, API keys, or login credentials, for "
            "your connected apps or anything else, and you must NEVER reveal, "
            "repeat, or guess a password or secret — even if one were to appear "
            "in this conversation, do not echo it back. If the user asks for a "
            "password or to log them in, say plainly that you don't keep "
            "credentials and point them to reset or view it in that app itself."
            + where + "]\n\n")


# The sandbox runtime's own preamble truthfully says outbound network is
# deny-by-default — but Ava's tools are host-mediated (they call the bridge,
# which does the network work), so without an affirmative counter-note the
# model concludes "no network → no web access" and denies capabilities it
# actually has. Registry-driven: new features in ava_bridge/features.py show
# up here with zero edits. Wording guarded by tests/test_tooling_note.py.
def _capabilities_note() -> str:
    from . import features
    try:
        feats = features.snapshot()
    except Exception:  # noqa: BLE001 — awareness must never break a turn
        feats = []
    if not feats:
        return ""
    states = "; ".join(f"{f['label']}: {'on' if f['enabled'] else 'OFF'}"
                       for f in feats)
    return (f"[note for {config.AVA_NAME} — not from the user: your tools are host-mediated — "
            "they call the host bridge, which does any network work on the "
            "host's side (web searches and page fetches egress via Tor there). "
            "The sandbox's own no-internet network policy does NOT mean you "
            "lack web access, so never answer \"I can't browse the web\" from "
            "that policy alone. Optional features right now: " + states + ". "
            "If a request needs a feature that is OFF, don't say you lack the "
            "ability — say that feature is switched off and can be enabled "
            "under Setup → System → Optional features. If unsure whether a "
            "capability works, call its tool and relay the result — a disabled "
            "feature returns a clear message with the fix.]\n\n")


def _tooling_note(direct: bool) -> str:
    """A one-paragraph awareness note prepended to the turn so Ava answers
    honestly about tools she does NOT have — instead of shrugging or
    hallucinating a tool call. Cases: the standing credentials stance, connected
    apps whose tools were never deployed into the sandbox, the tool-less
    direct runtime, and (sandbox runtime only) the affirmative capability
    states, so the sandbox's "no internet" preamble can't read as "no web"."""
    from . import connectors
    try:
        missing = connectors.undeployed()
    except Exception:  # noqa: BLE001 — awareness must never break a turn
        missing = []
    creds = _credentials_note()
    if direct:
        apps = ", ".join(m["label"] for m in missing) or None
        return creds + (f"[note for {config.AVA_NAME} — not from the user: the agent runtime is not "
                "active, so you have NO app tools this turn"
                + (f" (connected apps: {apps})" if apps else "")
                + ". If the question needs an app's data or actions, say so "
                "plainly and point the user to Setup → Agent to provision the "
                "runtime. Never invent tool results.]\n\n")
    if not missing:
        return creds + _capabilities_note()
    apps = ", ".join(f"{m['label']} ({m['tools']} tools)" for m in missing)
    return creds + _capabilities_note() + (
            f"[note for {config.AVA_NAME} — not from the user: these connected apps' tools "
            f"are NOT deployed to your sandbox yet: {apps}. You cannot use "
            "them this turn. If the user's request needs one of these apps, "
            "explain that they must open Setup → Connectors and click Deploy "
            "on that app first (Preview shows what gets loaded). Never invent "
            "tool results.]\n\n")


def _run_turn_direct(tid: str, agent_text: str, chat_id: str):
    """Degraded path: no agent runtime — talk to the inference router directly.
    No sandbox, so no live chain-of-thought polling; just the reply."""
    agent_text = _tooling_note(direct=True) + agent_text
    try:
        reply, tools = chat_direct(agent_text, history=history_for(chat_id))
    except Exception as e:  # noqa: BLE001
        with state.turns_lock:
            if tid in state.turns:
                state.turns[tid].update(status="error", error=str(e))
        return
    m = which_model()
    if chat_id:
        chat_append(chat_id, "assistant", reply, model=m)
    with state.turns_lock:
        if tid in state.turns:
            state.turns[tid].update(status="done", reply=reply, model=m,
                                    ctx_tokens=(m or {}).get("prompt_tokens"),
                                    tools_used=[])


def _run_turn(tid: str, agent_text: str, sid: str, chat_id: str):
    rt, err = runtime.gate()
    if err:  # agent.required is on but the runtime is missing — don't fake it
        with state.turns_lock:
            if tid in state.turns:
                state.turns[tid].update(status="error", error=err)
        return
    if not rt.supports_tools:  # direct floor (no sandbox, no live CoT)
        _run_turn_direct(tid, agent_text, chat_id)
        return
    after = _session_line_count(sid) + 1
    threading.Thread(target=_poll_turn_steps, args=(tid, sid, after), daemon=True).start()
    agent_text = _tooling_note(direct=False) + agent_text
    t0 = time.time()
    tools: list[str] = []
    try:
        reply, tools = ask_openclaw(agent_text, session_id=sid)
    except Exception as e:  # noqa: BLE001
        job = pickup_image_since(t0, wait=120)
        if job and chat_id:
            chat_append(chat_id, "assistant", "Here's the image you asked for.")
            attach_chat(job["id"], chat_id)  # bridge persists the image itself
        m = which_model()
        if job:
            _set_turn(
                    tid,
                    status="done", reply="Here's the image you asked for.",
                    job=job, model=m, ctx_tokens=(m or {}).get("prompt_tokens"),
                    tools_used=tools, error=None)
            return
        # No image to salvage: never leave the user with a dangling question and
        # an endless spinner. Give a plain, honest reply, persist it to the chat
        # so reopening the conversation shows what happened, and flag it degraded
        # so the UI can offer a one-tap retry.
        fallback = ("Sorry — I couldn't finish that just now (my tools timed "
                    "out or hit a snag). Please try again.")
        if chat_id:
            chat_append(chat_id, "assistant", fallback, model=m)
        # Recover whatever tools DID run before the failure so the flight
        # recorder shows the real actions, not an empty list (audit fidelity).
        partial_tools = _tools_from_session(sid, after)
        _set_turn(
                tid,
                status="done", reply=fallback, job=None, model=m,
                ctx_tokens=(m or {}).get("prompt_tokens"),
                tools_used=partial_tools, degraded=True, error=str(e))
        audit.record("turn", chat_id=chat_id, status="degraded",
                     tools=partial_tools, error=str(e)[:300], model=(m or {}).get("id"))
        return
    # `openclaw agent --json` doesn't reliably report tools; recover them from
    # the trajectory so tool chips AND the artifact panel (weather, etc.) work.
    if not tools:
        tools = _tools_from_session(sid, after)
    job = None
    if any("run_gpu_job" in t for t in tools):
        # run_gpu_job renders THROUGH the bridge now (real the GPU service progress),
        # so grab that live job; fall back to a file-watch if it isn't found.
        job = _latest_image_job_since(t0) or start_agent_image_watch(t0)
        if job and chat_id:
            # Bind the render to this chat: the bridge appends the image (or a
            # coded failure) when the job ends, even if the client is gone.
            attach_chat(job["id"], chat_id)
    previews = _pickup_previews_since(t0, tools)
    artifact = None
    try:
        artifact = build_turn_artifact(tools, sid, after)
    except Exception:  # noqa: BLE001 — the side panel is best-effort
        artifact = None
    m = which_model()
    final_steps = _read_session_steps(sid, after)  # durable CoT: definitive trajectory
    if chat_id:
        chat_append(chat_id, "assistant", reply, model=m, tools_used=tools,
                     steps=final_steps)
    with state.turns_lock:
        prev_steps = state.turns.get(tid, {}).get("steps")
    _set_turn(tid, status="done", reply=reply, job=job,
              previews=previews, artifact=artifact,
              model=m, ctx_tokens=(m or {}).get("prompt_tokens"),
              tools_used=tools,
              steps=final_steps or prev_steps)
    state.interaction["ts"] = time.time()  # turn finished — reset idle baseline
    audit.record("turn", chat_id=chat_id, status="done", tools=tools,
                 model=(m or {}).get("id"), duration_s=round(time.time() - t0, 1))


def start_turn(agent_text: str, sid: str, chat_id: str) -> str:
    _prune_turns()
    state.interaction["ts"] = time.time()  # mark interactive activity (idle-burn baseline)
    tid = uuid.uuid4().hex[:12]
    with state.turns_lock:
        state.turns[tid] = {"id": tid, "status": "running", "steps": [], "reply": None,
                            "job": None, "previews": [], "artifact": None, "model": None,
                            "ctx_tokens": None, "tools_used": [], "degraded": False,
                            "error": None, "created": time.time()}
    threading.Thread(target=_run_turn_guarded,
                     args=(tid, agent_text, sid, chat_id),
                     daemon=True).start()
    return tid


def _run_turn_guarded(tid: str, agent_text: str, sid: str, chat_id: str) -> None:
    """Run a turn and guarantee it reaches a terminal status.

    _run_turn has several `except Exception` handlers on the paths that were
    expected to fail, but nothing around the rest of it — and it is the target of
    a daemon thread, so anything it raises outside those handlers (a KeyError, a
    BaseException like MemoryError, a bug in the artifact builder) vanishes with
    the thread and leaves `status: "running"` in state.turns forever. The client
    polls that record until the tab is closed, and _poll_turn_steps spins a second
    thread on it for the same duration.

    BaseException rather than Exception on purpose: the point is that no exit path
    leaves the turn running, and a bare `raise` after recording keeps
    KeyboardInterrupt/SystemExit behaviour intact.
    """
    try:
        _run_turn(tid, agent_text, sid, chat_id)
    except BaseException as e:
        _set_turn(tid, status="error", error=f"{type(e).__name__}: {e}")
        raise
