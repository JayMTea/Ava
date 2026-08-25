"""Live "chain of thought" turns.

The `openclaw agent` CLI is non-streaming (one JSON blob at the end) and the
Nemotron model only supports thinking=off, so we can't stream reasoning tokens.
BUT the agent persists every step to its session jsonl as it works — including
{type:"thinking"} reasoning, intermediate {type:"text"} and {type:"toolCall"}
events. We run the (blocking) turn in a worker thread and concurrently poll the
tail of that session file, so the UI can show Ava's REAL reasoning + actions
live, then attach the final reply when she finishes.
"""
import json
import shlex
import threading
import time
import uuid

import requests

from . import audit, config, connectors, state, runtime, agent_media
from .agent import which_model, sbx_read, session_file, chat_direct
from .artifacts import build_turn_artifact
from .chat_store import chat_append, history_for


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
        # outlives max_age (a long agent turn; OC_TIMEOUT is up to 600s) could be
        # deleted mid-flight by an unrelated new message. Its completion then hit
        # a missing key and the client polled a turn id that had ceased to exist.
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
                "plainly and point the user to Setup → Agent → Runtime to provision the "
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
        # `code` rather than isinstance: any raiser that knows why it failed can
        # carry one, and the turn dict is returned verbatim by /api/turn/<id>, so
        # this is all it takes to reach frontend/src/lib/fixes.ts. Without it the
        # chat showed the router's own loopback URL and offered nowhere to go.
        with state.turns_lock:
            if tid in state.turns:
                state.turns[tid].update(status="error", error=str(e),
                                        error_code=getattr(e, "code", ""))
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
    """Resolve the runtime, then hand the turn to the path it can actually serve.

    Two paths, and the choice is a CAPABILITY question, never an isinstance one:
    a runtime that streams answers `supports_push_turns()`, and a fourth adapter
    added later gets the right path without editing this function.
    """
    rt, err = runtime.gate()
    if err:  # agent.required is on but the runtime is missing — don't fake it
        with state.turns_lock:
            if tid in state.turns:
                # `agent_off` / `agent_down` / `agent_conflict` — the same
                # convention every other capability follows, so the chat can
                # offer the fix rather than only the diagnosis.
                state.turns[tid].update(status="error", error=str(err),
                                        error_code=getattr(err, "code", ""))
        return
    if not rt.supports_tools:  # direct floor (no sandbox, no live CoT)
        _run_turn_direct(tid, agent_text, chat_id)
        return
    if rt.supports_push_turns():
        _run_turn_push(tid, agent_text, sid, chat_id, rt)
        return
    _run_turn_polled(tid, agent_text, sid, chat_id, rt)


def _run_turn_polled(tid: str, agent_text: str, sid: str, chat_id: str, rt):
    """The CLI path, unchanged: block on one call, tail the session file beside
    it for live chain-of-thought."""
    after = _session_line_count(sid) + 1
    threading.Thread(target=_poll_turn_steps, args=(tid, sid, after), daemon=True).start()
    agent_text = _tooling_note(direct=False) + agent_text
    t0 = time.time()
    tools: list[str] = []
    try:
        # The runtime `gate()` already resolved is the one that must serve the
        # turn. Routing through a hardcoded `runtime.nemoclaw()` here meant that
        # under `agent.runtime: remote` the gate admitted RemoteRuntime while the
        # reply came from the in-process CLI adapter — and `sbx_read`/
        # `session_file` (live CoT) correctly used `configured()`, so the
        # reasoning and the reply came from two different machines.
        reply, tools = rt.run_turn(agent_text, session_id=sid)
    except Exception as e:  # noqa: BLE001
        m = which_model()
        # Never leave the user with a dangling question and an endless spinner.
        # Give a plain, honest reply, persist it to the chat so reopening the
        # conversation shows what happened, and flag it degraded so the UI can
        # offer a one-tap retry.
        fallback = ("Sorry — I couldn't finish that just now (my tools timed "
                    "out or hit a snag). Please try again.")
        if chat_id:
            chat_append(chat_id, "assistant", fallback, model=m)
        # Recover whatever tools DID run before the failure so the flight
        # recorder shows the real actions, not an empty list (audit fidelity).
        partial_tools = _tools_from_session(sid, after)
        _set_turn(tid, status="done", reply=fallback, model=m,
                  ctx_tokens=(m or {}).get("prompt_tokens"),
                  tools_used=partial_tools, degraded=True, error=str(e))
        audit.record("turn", chat_id=chat_id, status="degraded",
                     tools=partial_tools, error=str(e)[:300], model=(m or {}).get("id"))
        return
    # `openclaw agent --json` doesn't reliably report tools; recover them from
    # the trajectory so tool chips AND the artifact panel (weather, etc.) work.
    if not tools:
        tools = _tools_from_session(sid, after)
    # durable CoT: the definitive trajectory, re-read once the turn is over
    _finish_turn(tid, chat_id, sid, after, reply, tools, t0,
                 final_steps=_read_session_steps(sid, after))


def _run_turn_push(tid: str, agent_text: str, sid: str, chat_id: str, rt):
    """The streaming path: start the run, then let its events drive the record.

    The shape is genuinely different from the polled path and that is the point.
    There, one blocking call returns a finished reply and a second thread tails a
    file beside it to guess at progress. Here the run announces itself
    (`start_run` returns immediately with an id) and every step arrives as an
    ordered event, so the chain-of-thought is a RECORD of what happened rather
    than an inference from a file that happens to be on disk.

    Three properties this path has to preserve, because the rest of the app
    already depends on them:

      * the turn always reaches a terminal status (`_run_turn_guarded` catches
        what escapes, but a hang is not an escape),
      * `steps` is written under `state.turns_lock` so `/api/turn/<id>` never
        reads a half-updated list,
      * a failure still persists a reply, because the alternative is a dangling
        question and an endless spinner.
    """
    agent_text = _tooling_note(direct=False) + agent_text
    t0 = time.time()
    tools: list[str] = []
    # Subscribe BEFORE starting. A short run can finish before `start_run`'s
    # response is even parsed, and a subscription opened afterwards would miss
    # the terminal event entirely.
    try:
        sub = rt.subscribe()
    except Exception as e:  # noqa: BLE001 — a runtime that changed its mind
        _fail_turn(tid, chat_id, sid, 0, t0, e, tools)
        return
    try:
        try:
            handle = rt.start_run(agent_text, session_id=sid,
                                  idempotency_key=f"turn:{tid}",
                                  thinking=config.OC_THINKING or None)
        except Exception as e:  # noqa: BLE001
            _fail_turn(tid, chat_id, sid, 0, t0, e, tools)
            return
        # Recorded so a reconnecting client — and `ava doctor` — can say WHICH
        # run a turn is waiting on, rather than only that it is waiting.
        _set_turn(tid, run_id=handle.run_id, session_id=handle.session_id)
        try:
            drained = _drain_run(tid, rt, sub, handle, t0)
        except Exception as e:  # noqa: BLE001
            # Recover the partial tool list from the PUBLISHED record rather
            # than from a local that the raising frame took with it. Every step
            # seen so far is already there — `_publish_steps` put it there — and
            # audit fidelity means the flight recorder shows the actions that
            # really ran, not an empty list because the turn ended badly.
            _fail_turn(tid, chat_id, sid, 0, t0, e, _tools_of_steps(_steps_so_far(tid)))
            return
    finally:
        sub.close()
    _finish_turn(tid, chat_id, sid, 0, drained["reply"], drained["tools"], t0,
                 final_steps=drained["steps"], attachments=drained["attachments"],
                 usage_tokens=drained["usage_tokens"], rt=rt)


def _drain_run(tid: str, rt, sub, handle, t0: float):
    """Read a run's progress until it ends, updating the live record as it goes.

    Note what is NOT here: any knowledge of the gateway's event names or payload
    shapes. `rt.iter_run()` yields Ava's four kinds, so this function reads the
    same way for any streaming runtime and an upstream rename never reaches the
    turn path.

    Returns (reply, tools, steps). Raises when the run fails or the iterator
    runs out without a final — the caller turns either into a persisted, honest
    failure.
    """
    steps: list[dict] = []
    tools: list[str] = []
    for ev in rt.iter_run(sub, handle, timeout=config.OC_TIMEOUT):
        kind = ev.get("kind")
        if kind == "gap":
            # Events were lost. Say so in the trajectory rather than silently
            # rendering a chain with a hole in it — the owner is entitled to
            # know the record is incomplete.
            steps.append({"kind": "text", "text": "(some steps were not received)"})
            _publish_steps(tid, steps)
        elif kind == "step":
            _fold_step(steps, ev.get("step") or {}, tools)
            _publish_steps(tid, steps)
        elif kind == "error":
            raise RuntimeError(str(ev.get("message") or "the run failed"))
        elif kind == "final":
            return {"reply": str(ev.get("text") or ""),
                    "tools": (ev.get("tools") or tools),
                    "steps": steps,
                    "attachments": list(ev.get("attachments") or []),
                    "usage_tokens": ev.get("usage_tokens")}
    raise TimeoutError(f"the agent did not finish within {config.OC_TIMEOUT}s")


def _fold_step(steps: list[dict], step: dict, tools: list[str]) -> None:
    """Add one step, folding a `tool_result` into its matching `tool` call.

    A tool call streams as a `tool` step (start) and, later, a `tool_result`
    (the output). Merging the result INTO the start — by `id`, then by name —
    is what turns "Using exec" x3 into one enriched card carrying its output and
    any media. The identical fold runs in `chatEvents.foldStep` for the live
    view, so the record and the stream never disagree. An orphan result (its
    start was never seen) becomes a standalone tool card rather than vanishing.
    """
    if step.get("kind") == "tool_result":
        target = _match_tool_step(steps, step)
        if target is not None:
            if step.get("output"):
                target["output"] = step["output"]
            if step.get("attachments"):
                target["attachments"] = step["attachments"]
            if step.get("is_error"):
                target["is_error"] = True
            return
        step = {**step, "kind": "tool"}
    steps.append(step)
    if step.get("kind") == "tool" and step.get("name") and step["name"] not in tools:
        tools.append(step["name"])


def _match_tool_step(steps: list[dict], result: dict) -> dict | None:
    cid = result.get("id")
    if cid:
        for s in reversed(steps):
            if s.get("kind") == "tool" and s.get("id") == cid:
                return s
    name = result.get("name")
    for s in reversed(steps):
        if (s.get("kind") == "tool" and s.get("name") == name
                and "output" not in s and "attachments" not in s):
            return s
    return None


def _publish_steps(tid: str, steps: list[dict]) -> None:
    """Hand the UI a COPY, under the lock.

    The live list keeps being appended to by this thread while `/api/turn/<id>`
    serialises whatever it finds — publishing the list itself is a mutation
    racing a read, and the symptom would be an occasional truncated or
    duplicated step rather than an exception.
    """
    with state.turns_lock:
        if tid in state.turns:
            state.turns[tid]["steps"] = list(steps)


def _steps_so_far(tid: str) -> list[dict]:
    """Whatever the live record holds right now, copied out under the lock."""
    with state.turns_lock:
        return list((state.turns.get(tid) or {}).get("steps") or [])


def _tools_of_steps(steps: list[dict]) -> list[str]:
    out: list[str] = []
    for st in steps or []:
        if st.get("kind") == "tool" and st.get("name") and st["name"] not in out:
            out.append(st["name"])
    return out


def _fail_turn(tid: str, chat_id: str, sid: str, after: int, t0: float,
               err: Exception, partial_tools: list[str]) -> None:
    """A failed turn still owes the owner a reply and the ledger a line.

    Same contract as the polled path's failure branch: never leave a dangling
    question and an endless spinner, persist what happened so reopening the
    conversation shows it, and flag it degraded so the UI can offer a retry.
    """
    m = which_model()
    fallback = ("Sorry — I couldn't finish that just now (my tools timed "
                "out or hit a snag). Please try again.")
    if chat_id:
        chat_append(chat_id, "assistant", fallback, model=m)
    _set_turn(tid, status="done", reply=fallback, model=m,
              ctx_tokens=(m or {}).get("prompt_tokens"),
              tools_used=partial_tools, degraded=True, error=str(err),
              error_code=getattr(err, "code", ""))
    audit.record("turn", chat_id=chat_id, status="degraded",
                 tools=partial_tools, error=str(err)[:300],
                 model=(m or {}).get("id"))


def _finish_turn(tid: str, chat_id: str, sid: str, after: int, reply: str,
                 tools: list[str], t0: float,
                 final_steps: list[dict] | None,
                 attachments: list[dict] | None = None,
                 usage_tokens: int | None = None, rt=None) -> None:
    """Everything a finished turn owes the rest of the app, for either path.

    Shared deliberately: previews, the artifact, media, the persisted message,
    the turn record and the audit line are the turn's CONTRACT with the UI and
    the ledger, not a detail of how the reply was obtained. Two copies would
    drift, and the drift would show up as a chat that renders differently
    depending on which runtime answered — the exact class of bug the runtime
    seam exists to prevent.
    """
    previews = _pickup_previews_since(t0, tools)
    artifact = None
    try:
        # `final_steps` carries this turn's tool arguments on the streaming
        # path, so the builder never has to reach into a sandbox that may not
        # have a session file at all.
        artifact = build_turn_artifact(tools, sid, after, steps=final_steps)
    except Exception:  # noqa: BLE001 — the side panel is best-effort
        artifact = None
    # Resolve any media the reply or a tool produced into same-origin, seekable
    # URLs. Best-effort and non-blocking to the reply: an unresolvable ref is
    # dropped, never rendered as a broken player.
    media = _resolve_turn_media(rt, attachments, final_steps)
    m = which_model()
    # The gateway's own usage count is the honest numerator; fall back to the
    # local router's estimate only when the gateway did not report one.
    ctx_tokens = usage_tokens if usage_tokens else (m or {}).get("prompt_tokens")
    if chat_id:
        chat_append(chat_id, "assistant", reply, model=m, tools_used=tools,
                    steps=final_steps, attachments=media)
    with state.turns_lock:
        prev_steps = state.turns.get(tid, {}).get("steps")
    _set_turn(tid, status="done", reply=reply,
              previews=previews, artifact=artifact, attachments=media or [],
              model=m, ctx_tokens=ctx_tokens,
              tools_used=tools,
              steps=final_steps or prev_steps)
    state.interaction["ts"] = time.time()  # turn finished — reset idle baseline
    audit.record("turn", chat_id=chat_id, status="done", tools=tools,
                 model=(m or {}).get("id"), duration_s=round(time.time() - t0, 1))


def _resolve_turn_media(rt, attachments: list[dict] | None,
                        steps: list[dict] | None) -> list[dict] | None:
    """Resolve reply-level media, and rewrite each tool step's own media in
    place, to same-origin URLs. Returns the reply-level list (already bounded)."""
    try:
        for st in steps or []:
            if isinstance(st, dict) and st.get("attachments"):
                st["attachments"] = agent_media.resolve_refs(rt, st["attachments"])
        reply_media = agent_media.resolve_refs(rt, attachments)
        return agent_media.bound_attachments(reply_media)
    except Exception:  # noqa: BLE001 — media never breaks a reply
        return None


def start_turn(agent_text: str, sid: str, chat_id: str) -> str:
    """Register a turn and run it on a worker thread. Returns the turn id at once.

    NON-BLOCKING: the reply is not ready when this returns. The caller polls
    /api/turn/<id> until status leaves "running". Two daemon threads are spawned —
    the turn itself and a poller that tails the agent's session file so the UI can
    show live chain-of-thought.

    The turn is guaranteed to reach a terminal status ("done" or "error"): see
    _run_turn_guarded. Old finished turns are pruned here, on the way in.
    """
    _prune_turns()
    state.interaction["ts"] = time.time()  # mark interactive activity (idle-burn baseline)
    tid = uuid.uuid4().hex[:12]
    with state.turns_lock:
        state.turns[tid] = {"id": tid, "status": "running", "steps": [], "reply": None,
                            "previews": [], "artifact": None, "attachments": [],
                            "model": None,
                            "ctx_tokens": None, "tools_used": [], "degraded": False,
                            "error": None, "created": time.time(),
                            # Set by the streaming path only. Present-and-None on
                            # every turn rather than absent on some, because
                            # /api/turn/<id> returns this dict verbatim and a key
                            # that comes and goes is a shape the client has to
                            # guard instead of read.
                            "run_id": None, "session_id": None}
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
        _set_turn(tid, status="error", error=f"{type(e).__name__}: {e}",
                  error_code=getattr(e, "code", ""))
        raise


def abort_turn(tid: str) -> dict:
    """Ask the runtime to stop a running turn.

    Returns a small verdict dict rather than raising, because every outcome
    here is ordinary and the caller renders all of them: the turn may have
    finished a moment ago (a race the owner cannot see coming and should not be
    shown an error for), the runtime may not support stopping, or the ask may
    be refused by the gateway.

    What this does NOT do is write the turn's status. The run's own ending
    arrives as an event carrying `aborted`, and the existing terminal-status
    writer records it — see `AgentRuntime.abort_run`. Two writers racing over
    one turn is how a completed turn gets relabelled "stopped".
    """
    from . import runtime
    with state.turns_lock:
        rec = state.turns.get(tid)
        if rec is None:
            return {"ok": False, "code": "turn_unknown",
                    "error": "that turn is not on record any more"}
        if rec.get("status") != "running":
            # Not an error: the turn ended between the click and the request.
            return {"ok": True, "code": "already_finished",
                    "status": rec.get("status")}
        sid = rec.get("session_id")
        run_id = rec.get("run_id") or ""
    if not sid:
        # The polled/direct paths have no run to stop — there is no id because
        # nothing announced itself. Say so plainly instead of pretending.
        return {"ok": False, "code": "abort_unsupported",
                "error": "this runtime cannot stop a turn once it has started"}
    rt = runtime.configured()
    if not rt.supports_abort():
        return {"ok": False, "code": "abort_unsupported",
                "error": "this runtime cannot stop a turn once it has started"}
    try:
        rt.abort_run(sid, run_id)
    except Exception as e:  # noqa: BLE001 — every failure here is reportable
        return {"ok": False, "code": "abort_failed", "error": str(e)}
    # "asked", not "stopped": the ending is still the event stream's to report.
    return {"ok": True, "code": "asked", "run_id": run_id or None}
