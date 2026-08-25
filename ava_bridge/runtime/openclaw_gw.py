"""OpenClawGatewayRuntime — the agent, over its own control plane.

`NemoClawRuntime` drives OpenClaw by spawning `openclaw agent --json` inside the
sandbox once per message. That call can carry exactly one thing: a reply and a
tool list. This adapter talks to the same OpenClaw over its gateway instead —
JSON-RPC 2.0 on a persistent WebSocket — so a turn streams, and the other ~200
methods (sessions, forking, rewind, cron, devices, plugins, approvals, audit)
become reachable at all.

It is a SEPARATE runtime, selected with `agent.runtime: openclaw_gw`. The CLI
adapter is untouched and stays the default; the `openclaw` alias still points at
it. Silently repointing a name somebody already set is the one thing
`name_error()`'s docstring says must not happen quietly.

WHAT THIS RUNTIME CANNOT DO
---------------------------
It observes and controls; it does not deploy. `agent/install.sh` is what pushes
bytes into the sandbox, and it needs the `nemoclaw` CLI. So `provision()`
delegates to the CLI adapter when one is present and refuses honestly when it is
not — the same shape and the same reasoning as `DirectRuntime.provision`. A
cheerful `ok: True` to an owner who just clicked Apply is a small lie.

The five ABC sandbox probes (`read_file`, `digest`, `glob_digest`,
`tree_digests`, `remove_policy`) are built on `exec()`, which the gateway does
not offer as a shell. They degrade to `unknown` — "we could not look" — which is
the honest answer and is never counted as pending drift.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import time
import uuid

from .. import config
from . import nemoclaw_registry, openclaw_gw_client
from .base import AgentRuntime, RunHandle
from .errors import GatewayError, GatewayUnsupported

# ---------------------------------------------------------------------------
# The run-event vocabulary.
#
# These are OpenClaw's names, and they are the one part of this module Ava does
# not control. They are collected here rather than scattered so that a rename
# upstream is a one-line change with a test that names it
# (`qa/fakes/fake_gateway.py` scripts exactly these).
#
# The important design decision is what happens when they are WRONG: nothing
# below blocks forever on seeing a terminal event. `_await_run` falls back to
# reconciling against `chat.history`, so an unrecognised event name makes a turn
# slower, never permanently stuck. Failing toward "works" is the whole point —
# a hung turn with a spinner is the worst outcome available, and it is exactly
# what a hardcoded event name buys you the first time upstream renames one.
# ---------------------------------------------------------------------------
# The LIVE vocabulary, verified against OpenClaw 2026.7.1 over a real streamed
# run on 2026-08-23. There are four event names, not a dozen, and the state is
# carried in the PAYLOAD rather than encoded in the name:
#
#   agent   stream=lifecycle   data.phase: start | finishing | end
#                              (+ stopReason, aborted, livenessState, endedAt)
#   agent   stream=assistant   data: {text, delta}      cumulative + increment
#   agent   stream=tool        data: the tool call      (needs caps:[tool-events])
#   chat    state: delta       deltaText + cumulative `message`
#   chat    state: final       stopReason + final `message`
#   health, tick               housekeeping, never run events
#
# An earlier version of this table listed `run.finished`, `run.step`,
# `chat.delta` and friends, taken from prose docs. NONE of those names are ever
# emitted, so every turn hung until the reconcile fallback and no step ever
# rendered. If you are tempted to add a dotted name here, capture a live run
# first: `type:"event"` frames carry the name in `event`, not `topic`.
_EV_AGENT = "agent"
_EV_CHAT = "chat"
_EV_HOUSEKEEPING = ("health", "tick")
_EV_GAP = "ava.gateway.gap"

# `chat`/delta and `agent`/assistant carry THE SAME text, one as `deltaText` and
# one as `data.delta`. Rendering both doubles every reply, so assistant text is
# taken from `chat` only, and the `agent` stream contributes the material `chat`
# does not have: tool calls and reasoning.
_AGENT_STEP_STREAMS = ("tool", "tools", "reasoning", "thinking")

# A captured tool result is bounded before it enters the trajectory: OpenClaw
# already caps its own tool-event text (~8000 chars), and chat_store trims again
# on persist, so this is the live ceiling on one tool card's output.
_TOOL_OUTPUT_MAX = 4000
# The upstream `MEDIA:` reply convention (docs.openclaw.ai/tools/media-overview):
# a line that is exactly `MEDIA: <ref>`, optionally backtick-wrapped. Captured
# from the 2026.7.1 Control UI parser, which accepts http(s), data:, /media/,
# /__openclaw__/, media://, file:// and absolute/relative paths.
_MEDIA_LINE = re.compile(r"^\s*MEDIA:\s*`?([^`\n]+?)`?\s*$", re.IGNORECASE | re.MULTILINE)


def _classify(topic: str, payload: dict) -> str | None:
    """One place that answers "what kind of thing is this event".

    Returns Ava's kind — step | final | error | gap — or None for a frame that
    is not part of a run. Both `iter_run` and `translate_event` call this, so
    the turn path and the browser relay cannot drift apart.
    """
    if topic == _EV_GAP:
        return "gap"
    if topic in _EV_HOUSEKEEPING:
        return None
    if topic == _EV_CHAT:
        state = str(payload.get("state") or "")
        if state == "final":
            return "final"
        if state in ("error", "failed"):
            return "error"
        if state == "delta":
            return "step"
        return None
    if topic == _EV_AGENT:
        stream = str(payload.get("stream") or "")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if stream == "lifecycle":
            # `end` is NOT a final: the text rides on chat/final, which may not
            # have arrived yet. Only a failed ending is news here.
            if str(data.get("phase") or "") == "end":
                if data.get("aborted") or str(data.get("stopReason") or "") in (
                        "error", "failed", "cancelled", "canceled"):
                    return "error"
            return None
        if stream in _AGENT_STEP_STREAMS:
            return "step"
    return None

_RECONCILE_EVERY_S = 5.0

# A socket that has never been dialled reports the SAME "down" a dead gateway
# does, so these cannot be told apart by phase — only by whether this process
# has tried yet. Hence the one-shot wait in `available()`.
_NOT_YET_PHASES = ("down", "starting", "connecting", "handshaking")
_COLD_START_WAIT_S = 4.0

# Mirrors provision.PERSONA_PATH. Named here rather than imported because
# reaching into that module for a constant would make the runtime depend on the
# provisioner, and the dependency runs the other way.
PERSONA_PATH = "/sandbox/.openclaw/workspace/IDENTITY.md"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class OpenClawGatewayRuntime(AgentRuntime):
    name = "openclaw_gw"
    display_name = "OpenClaw gateway"

    def blurb(self) -> str:
        return ("Talks to the same OpenClaw sandbox over its gateway "
                "WebSocket instead of spawning a CLI per message — that is "
                "what streams a turn and makes sessions, automations and "
                "approvals reachable.")

    def install_hint(self) -> str | None:
        return ("The sandbox comes from `nemoclaw onboard`; the gateway needs "
                "an operator token at Setup -> Agent -> Runtime.")

    def __init__(self, client=None):
        self._client = client or openclaw_gw_client.client()
        self._avail_cache: dict = {"ts": 0.0, "ok": False, "cold_waited": False}

    # ---- capability flags ---------------------------------------------------
    # Properties, not class attributes, ON THIS RUNTIME ONLY. `turns.py:318` and
    # `agent.runtime_available()` read these names off whatever object they hold;
    # Python resolves the lookup through the type, so the other three adapters
    # keep their plain booleans and neither call site changes. They are a
    # CONTRACT (a bool, answered honestly), not necessarily a constant — do not
    # "simplify" this back into a class attribute.
    @property
    def supports_tools(self) -> bool:
        # chat.send is what carries tool use; a gateway without it cannot serve a
        # turn at all, let alone a tool-using one.
        return "chat.send" in self._client.methods()

    @property
    def supports_cot(self) -> bool:
        return bool({"sessions.messages.subscribe", "sessions.subscribe"}
                    & self._client.methods())

    def supports_push_turns(self) -> bool:
        return self.supports_tools and self.supports_cot

    #: Ava's decisions -> the gateway's. Captured live: each row also carries
    #: its own `allowedDecisions`, and on this build they are these three.
    _DECISIONS = {"approve": "allow-once", "always": "allow-always",
                  "deny": "deny"}

    def control_plane(self):
        """This runtime IS a control-plane client; hand it over."""
        return self._client

    def identity(self) -> dict | None:
        """Gateway + agent identity, and how many devices are paired.

        Every field here is captured from a live gateway. The device id is a
        PUBLIC key fingerprint the gateway already shows its own operators — it
        identifies, it does not authenticate — and the public key itself is
        deliberately NOT carried: nothing in Ava needs it, and a value that
        looks like a key invites being treated as one.
        """
        methods = self._client.methods()
        out: dict = {}

        def ask(method, params=None):
            if method not in methods:
                return None
            try:
                return self._client.rpc(method, params or {}, timeout=8.0)
            except GatewayError:
                return None

        got = ask("gateway.identity.get")
        if isinstance(got, dict) and got.get("deviceId"):
            # Short form: a 64-char hex fingerprint is unreadable in a UI row,
            # and the operator is comparing it, not transcribing it.
            out["device_id"] = str(got["deviceId"])[:12]

        got = ask("agent.identity.get")
        if isinstance(got, dict):
            if got.get("agentId"):
                out["agent_id"] = str(got["agentId"])
            if got.get("name"):
                out["agent_name"] = str(got["name"])

        got = ask("device.pair.list")
        if isinstance(got, dict):
            paired = got.get("paired")
            pending = got.get("pending")
            out["paired"] = len(paired) if isinstance(paired, list) else None
            out["pending"] = len(pending) if isinstance(pending, list) else None

        got = ask("agents.list")
        if isinstance(got, dict):
            rows = got.get("agents")
            if isinstance(rows, list):
                out["agents"] = len(rows)

        return out or None

    def pending_approvals(self) -> list[dict]:
        """Exec approvals waiting on a human, as Ava's banner rows.

        POLLED, not evented: subscribing while an approval was pending produced
        no events at all (verified live) — the topic vocabulary is
        agent/chat/health/tick and approvals are not in it.
        """
        if "exec.approval.list" not in self._client.methods():
            return []
        try:
            got = self._client.rpc("exec.approval.list", {}, timeout=5.0)
        except GatewayError:
            return []
        # A bare LIST when anything is pending, an empty object when not —
        # captured live. It is NOT {approvals: [...]}.
        rows = got if isinstance(got, list) else []
        out = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            req = row.get("request") if isinstance(row.get("request"), dict) else {}
            allowed = req.get("allowedDecisions")
            allowed = allowed if isinstance(allowed, list) else []
            decisions = [k for k, v in self._DECISIONS.items() if v in allowed]
            out.append({
                "id": str(row["id"]),
                "source": "agent",
                "connector": "agent",
                "action": str(req.get("command") or "(no command)"),
                "args": {},
                "ts": (row.get("createdAtMs") or 0) / 1000.0,
                "decisions": decisions or ["approve", "deny"],
                "grantable": "always" in decisions,
                "warning": str(req.get("warningText") or "") or None,
            })
        return out

    def resolve_approval(self, approval_id: str, decision: str) -> bool:
        want = self._DECISIONS.get(decision)
        if want is None:
            return False
        try:
            self._client.rpc("exec.approval.resolve",
                             {"id": approval_id, "decision": want}, timeout=10.0)
        except GatewayError:
            return False        # it may have expired while sitting on screen
        return True

    def supports_abort(self) -> bool:
        """`chat.abort` -> {sessionKey} (+ optional runId), captured live."""
        return "chat.abort" in self._client.methods()

    def abort_run(self, session_id: str, run_id: str = "") -> bool:
        """Stop a run in flight. The ENDING still arrives as an event.

        Params are captured, not guessed: `sessionKey` is required and `runId`
        optional. The key sent is the same UNPREFIXED one `chat.send` was given
        — the gateway echoes a PREFIXED form back in its frames
        (`agent:main:<key>`), and handing that back is not what it accepts.

        Returns True when the ask was accepted. It deliberately does not wait
        for, or report, the ending: that is the event stream's job.
        """
        if not self.supports_abort():
            raise GatewayUnsupported(self.name)
        params: dict = {"sessionKey": session_id}
        if run_id:
            params["runId"] = run_id
        self._client.rpc("chat.abort", params, timeout=15.0)
        return True

    # ---- availability -------------------------------------------------------
    def available(self) -> bool:
        """Ready to serve a turn right now.

        `config.AGENT_ENABLED` is checked first for the same reason the other two
        tool runtimes check it: the master switch must win before anything dials
        a socket, or turning the agent off still costs a connection attempt on
        every turn.
        """
        if not config.AGENT_ENABLED:
            return False
        now = time.time()
        c = self._avail_cache
        if now - c["ts"] < 5.0:
            return bool(c["ok"])
        self._client.start()
        phase = self._client.status().get("phase")
        if not c["cold_waited"]:
            # ONCE per process, and only the first time. `start()` is
            # non-blocking, so on the first call the socket cannot be ready yet
            # — the phase is still the same "down" a dead gateway reports.
            # Caching that False pinned every caller to "unavailable" for the
            # cache window, which a long-running bridge shrugs off on the next
            # turn but a short-lived one never does: `ava agent status` printed
            # `active: direct` against a perfectly healthy gateway, every time.
            #
            # The flag is set BEFORE the wait, not after a success, so a gateway
            # that is genuinely down costs this delay exactly once rather than
            # on every turn for the life of the process.
            c["cold_waited"] = True
            deadline = time.time() + _COLD_START_WAIT_S
            while time.time() < deadline and phase in _NOT_YET_PHASES:
                time.sleep(0.1)
                phase = self._client.status().get("phase")
        ok = phase == "ready" and self.supports_tools
        c.update(ts=now, ok=ok)
        return ok

    def live(self) -> dict:
        st = self._client.status()
        return {"live": st.get("phase") == "ready",
                "reason": "" if st.get("phase") == "ready" else (st.get("why") or "")}

    def sandbox_info(self, wait: bool = True) -> dict | None:
        """{model, provider} for this sandbox — the shape `effective_brain` reads.

        WHY THIS EXISTS: `models.effective_brain()` resolves "what is Ava
        thinking with" by asking the ACTIVE RUNTIME first, via
        `getattr(rt, "sandbox_info", None)`. This runtime did not have the
        method, so that returned None, the model id came out empty, and every
        surface reading it — /api/health, the chat header, Setup → Agent, the
        hardware panel — announced "No model is configured, so there is nothing
        to answer with" while a correctly configured model was answering turns.

        The branch tolerates an empty id on the grounds that it is
        "self-correcting — the name appears when the background refresh lands".
        That holds for NemoClawRuntime, which shells out to `nemoclaw list
        --json` on a 120s cache. It does not hold here: with no method at all
        there was no refresh to land, so empty was permanent.

        No shell-out is needed either way. `nemoclaw_registry` already reads and
        caches the same facts straight from the registry file, which is cheap
        enough for the public /api/health — so `wait` is accepted for signature
        compatibility and simply not needed.
        """
        rec = nemoclaw_registry.registry_record(config.OC_SANDBOX) or {}
        model = str(rec.get("model") or "").strip()
        if not model:
            # None, not {}: "I do not know" is different from "there is none",
            # and the caller's fallback copy is written for the former.
            return None
        return {"model": model,
                "provider": str(rec.get("provider") or "").strip()}

    # ---- control plane ------------------------------------------------------
    def capabilities(self) -> list[str]:
        """What this ADAPTER offers, as opposed to what the gateway offers."""
        return ["gateway.rpc", "gateway.events", "gateway.push_turns"]

    def rpc_methods(self) -> frozenset[str]:
        return self._client.methods()

    def rpc(self, method: str, params: dict | None = None, *,
            timeout: float = 30.0, idempotency_key: str | None = None) -> dict:
        return self._client.rpc(method, params, timeout=timeout,
                                idempotency_key=idempotency_key)

    def subscribe(self, topics=None, *, maxlen: int = 1000):
        return self._client.subscribe(topics, maxlen=maxlen)

    # ---- turns --------------------------------------------------------------
    def start_run(self, text: str, *, session_id: str, idempotency_key: str,
                  thinking: str | None = None) -> RunHandle:
        """`chat.send` answers with an id immediately; the reply arrives later.

        The idempotency key is the caller's, not ours: only the caller knows what
        "the same logical operation" means. `turns.py` passes `turn:<tid>`, so a
        send retried across a reconnect cannot start two runs.
        """
        # Param names verified live: chat.send takes sessionKey/message, not
        # sessionId/text, and its schema REFUSES unexpected properties — so a
        # `thinking` key here would fail every turn at validation. The reasoning
        # budget is a router-side concern (`inference.reasoning` in ava.yaml),
        # which is why dropping it costs nothing.
        params: dict = {
            "sessionKey": session_id,
            "message": text,
            # deliver:false keeps the reply on the socket instead of pushing it
            # out through a messaging channel.
            "deliver": False,
            "timeoutMs": int((config.OC_TIMEOUT or 120) * 1000),
            "idempotencyKey": idempotency_key,
        }
        got = self._client.rpc("chat.send", params,
                               idempotency_key=idempotency_key)
        run_id = str(got.get("runId") or got.get("id") or "")
        if not run_id:
            raise GatewayError(
                "chat.send did not return a run id, so this turn cannot be "
                "followed", "gateway_rpc_failed", detail={"payload": got})
        return RunHandle(run_id=run_id, session_id=session_id,
                         idempotency_key=idempotency_key, extra=got)

    def run_turn(self, text: str, session_id: str | None = None,
                 history: list[dict] | None = None) -> tuple[str, list[str]]:
        """One turn, blocking — the ABC's shape, kept working.

        `turns.py` uses the streaming path when `supports_push_turns()`, but
        `/api/talk`, `/api/talk-text` and `warm()` all call `run_turn` and want a
        finished answer. This runs the same gateway machinery and simply waits.
        """
        sid = session_id or config.OC_SESSION
        # Subscribe BEFORE sending. A run can finish faster than the response to
        # chat.send is parsed, and a subscription opened afterwards would miss
        # the terminal event and fall all the way through to reconciliation.
        sub = self._client.subscribe()
        try:
            handle = self.start_run(text, session_id=sid,
                                    idempotency_key=f"turn:{uuid.uuid4().hex[:12]}",
                                    thinking=config.OC_THINKING or None)
            return self._await_run(sub, handle, timeout=config.OC_TIMEOUT)
        finally:
            sub.close()

    def iter_run(self, sub, handle: RunHandle, timeout: float | None = None):
        """Yield a run's progress in AVA's vocabulary, not OpenClaw's.

        This is the seam doing its job. `turns.py` must not learn the gateway's
        event names — if it did, every rename upstream would reach into the turn
        path, and a second runtime with different names could never be added
        without editing it. So the wire format stops here and what comes out is
        four kinds Ava already understands:

            {"kind": "step",  "step": {kind, text|name, args?}}
            {"kind": "final", "text": str, "tools": [str]}
            {"kind": "error", "message": str, "code": str}
            {"kind": "gap"}                 events were lost; nothing inferred

        The iterator ends when the run ends. It does NOT raise on timeout —
        exhausting without a `final` is the timeout, which lets the caller decide
        what a partial turn is worth rather than having that decided here.
        """
        deadline = time.time() + (timeout or config.OC_TIMEOUT)
        next_reconcile = time.time() + _RECONCILE_EVERY_S
        tools: list[str] = []
        while time.time() < deadline:
            ev = sub.get(timeout=0.5)
            if ev is not None:
                topic = ev.get("topic") or ""
                payload = ev.get("payload") or {}
                kind = _classify(topic, payload)
                if kind == "gap":
                    # Go and read the authoritative record rather than inferring
                    # anything from the events that did arrive.
                    next_reconcile = 0.0
                    yield {"kind": "gap"}
                elif kind is None or not self._is_ours(payload, handle):
                    pass
                elif kind == "step":
                    step = _step_of(payload)
                    if step:
                        if step["kind"] == "tool" and step["name"] not in tools:
                            tools.append(step["name"])
                        yield {"kind": "step", "step": step}
                elif kind == "error":
                    # A STOPPED run is not a failed one. The lifecycle-end
                    # frame carries no `message` at all (captured fields:
                    # stopReason, aborted, livenessState, phase, endedAt), so
                    # without this it fell through to "the run failed" and the
                    # owner was told their own Stop button was an error.
                    data = (payload.get("data")
                            if isinstance(payload.get("data"), dict) else {})
                    if data.get("aborted"):
                        yield {"kind": "error", "message": "stopped",
                               "code": "run_aborted"}
                        return
                    yield {"kind": "error",
                           "message": str(payload.get("message")
                                          or "the run failed"),
                           "code": str(payload.get("code") or "")}
                    return
                elif kind == "final":
                    text = _final_text(payload)
                    if text is not None:
                        atts = _final_media(payload)
                        # A recognised `MEDIA: <ref>` line renders as a card, so
                        # drop it from the prose exactly as the Control UI does —
                        # a raw sandbox path is not something to read (or speak).
                        yield {"kind": "final", "text": _strip_media_lines(text),
                               "tools": tools or _tools_of(payload),
                               "attachments": atts,
                               "usage_tokens": _usage_tokens(payload)}
                        return
                    next_reconcile = 0.0
            if time.time() >= next_reconcile:
                done = self._reconcile(handle)
                if done is not None:
                    reply, hist_tools, atts, usage = done
                    yield {"kind": "final", "text": reply,
                           "tools": tools or hist_tools,
                           "attachments": atts, "usage_tokens": usage}
                    return
                next_reconcile = time.time() + _RECONCILE_EVERY_S

    def _await_run(self, sub, handle: RunHandle,
                   timeout: float) -> tuple[str, list[str]]:
        """The blocking shape, built on the streaming one so there is only ever
        one description of how a run ends."""
        tools: list[str] = []
        for ev in self.iter_run(sub, handle, timeout=timeout):
            if ev["kind"] == "step" and ev["step"]["kind"] == "tool":
                name = ev["step"]["name"]
                if name not in tools:
                    tools.append(name)
            elif ev["kind"] == "error":
                raise GatewayError(ev["message"], "gateway_rpc_failed",
                                   gw_code=ev.get("code") or "")
            elif ev["kind"] == "final":
                return ev["text"], (ev["tools"] or tools)
        raise GatewayError(
            f"the agent did not finish within {timeout:g}s", "gateway_timeout",
            retryable=True, detail={"runId": handle.run_id})

    def _reconcile(self, handle: RunHandle) -> tuple[str, list[str], list[dict], int | None] | None:
        """The authoritative answer: has this run's reply landed in the session?

        Also the reconnect story — a socket that dropped mid-run loses events but
        not the transcript, so the same read that covers a renamed event covers a
        closed laptop lid.

        HOW A REPLY IS IDENTIFIED (captured live, OpenClaw 2026.7.1)
        -----------------------------------------------------------
        This used to require `msg["runId"] == handle.run_id`, and **no message
        in a transcript has a `runId` at all**. An assistant message carries
        `__openclaw, api, content, model, provider, responseId, role,
        stopReason, timestamp, usage` — and that is the whole list. So the match
        never fired, this returned None every single time, and the recovery path
        the module header promises has never once run.

        What IS on record is the `idempotencyKey` of the **user** message —
        the key Ava sent, which the gateway also adopts as the run id. So a
        reply is identified by its ANCHOR: the assistant message that directly
        follows our user message. `runId` is still preferred if a future build
        starts emitting one, so this gets better on its own rather than being
        pinned to today's shape.
        """
        if "chat.history" not in self._client.methods():
            return None
        try:
            # `sessionKey`, never `sessionId` — captured live: the chat.history
            # schema REFUSES sessionId, and accepts the short key
            # ('ava-phone-x') as well as the full 'agent:main:...' form. With
            # the wrong name every call failed validation, the except below ate
            # it, and this silently returned None.
            got = self._client.rpc("chat.history",
                                   {"sessionKey": handle.session_id, "limit": 8},
                                   timeout=15.0)
        except GatewayError:
            return None

        # A run still going is not an answer. `inFlightRun` is {runId, text} —
        # the reply accumulating live — and reading `messages` while it is set
        # returns the PREVIOUS turn, which is how a reconnect answers a question
        # with the reply to the one before it.
        infl = got.get("inFlightRun")
        if isinstance(infl, dict) and infl.get("runId"):
            if str(infl["runId"]) == handle.run_id:
                return None

        msgs = _messages_of(got)
        key = handle.idempotency_key or handle.run_id
        for i in range(len(msgs) - 1, -1, -1):
            msg = msgs[i]
            if str(msg.get("role") or "") != "assistant":
                continue
            if msg.get("pending") or msg.get("streaming"):
                return None
            # 1. A runId, if this build ever grows one.
            run = str(msg.get("runId") or "")
            if run:
                if run == handle.run_id:
                    return (_text_of(msg), _tools_of(msg),
                            _final_media(msg), _usage_tokens(msg))
                continue
            # 2. The anchor: is the message before it OUR user message?
            prev = msgs[i - 1] if i else None
            if not isinstance(prev, dict):
                continue
            if str(prev.get("role") or "") != "user":
                continue
            # The stored key is SUFFIXED by role: sending `turn:abc` records
            # `turn:abc:user` on the user message. Captured live — an equality
            # test here matches nothing at all, which is how the first version
            # of this fix still failed.
            stored = str(prev.get("idempotencyKey") or "")
            if key and stored and (stored == key
                                   or stored.rsplit(":", 1)[0] == key):
                return (_text_of(msg), _tools_of(msg),
                        _final_media(msg), _usage_tokens(msg))
        return None

    @staticmethod
    def _is_ours(payload: dict, handle: RunHandle) -> bool:
        """Filter by run, then by session.

        The gateway broadcasts to every operator connection, so an unfiltered
        reader would happily finish this turn on somebody else's reply — and a
        payload with neither id is not evidence about this run either way.
        """
        run = payload.get("runId") or payload.get("run_id")
        if run:
            return str(run) == handle.run_id
        sess = payload.get("sessionId") or payload.get("session_id")
        return bool(sess) and str(sess) == handle.session_id

    # ---- session lifecycle --------------------------------------------------
    def discard_session(self, session_id: str) -> bool:
        """Ghost mode. `sessions.delete` when the gateway has it; otherwise say
        no rather than returning True for work that did not happen — the caller
        is deciding whether a conversation left a trace."""
        if not session_id or "sessions.delete" not in self._client.methods():
            return False
        try:
            # `{key}`, verified live — sessions.delete refuses `sessionKey`
            # AND `sessionId`, and answers {ok, key: 'agent:main:<short>',
            # deleted}. This used to send `sessionId`, so every ghost-mode
            # discard failed validation, the except below turned it into
            # False, and THE GATEWAY SESSION WAS NEVER DELETED: the "leaves
            # no trace" promise on /api/ghost/discard silently did not hold
            # for this runtime. The short key is accepted as-is.
            self._client.rpc("sessions.delete", {"key": session_id},
                             timeout=15.0)
            return True
        except GatewayError:
            return False

    def warm(self) -> None:
        # Opening the socket IS the cold-start cost here; a throwaway turn would
        # add a real message to a real session, which the CLI adapter could get
        # away with and this one cannot.
        self._client.start()

    # ---- sandbox probes -----------------------------------------------------
    def exec(self, inner: str, timeout: int = 20) -> str:
        """No shell. The gateway is a control plane, not a container.

        Returning "" (rather than raising) is what the ABC's probes expect: they
        read an empty answer as "we could not look", which `provision.item_state`
        renders as `unknown` and never counts as pending. See the module
        docstring — this is a documented degradation, not an oversight.
        """
        return ""

    def session_file(self, session_id: str) -> str | None:
        """Live chain-of-thought comes from events here, not from tailing a file
        inside the sandbox. Nothing to point at, and pointing at the CLI
        adapter's path would be a lie about where the truth lives."""
        return None

    def registry_record(self) -> dict | None:
        return nemoclaw_registry.registry_record()

    def translate_event(self, topic: str, payload: dict) -> dict | None:
        """The same table `iter_run` reads, applied to one relayed frame.

        Deliberately shares `_EV_STEP` / `_EV_FINAL` / `_EV_ERROR` rather than
        keeping a second list: a rename upstream then lands in one place, and
        the browser and the turn path cannot disagree about what a `final` is.
        """
        kind = _classify(topic, payload)
        if kind == "step":
            step = _step_of(payload)
            return {"kind": "step", "step": step} if step else None
        if kind == "error":
            return {"kind": "error",
                    "message": str(payload.get("message") or "the run failed"),
                    "code": str(payload.get("code") or "")}
        if kind == "final":
            text = _final_text(payload)
            if text is None:
                return None
            return {"kind": "final", "text": _strip_media_lines(text),
                    "tools": _tools_of(payload),
                    "attachments": _final_media(payload)}
        if kind == "gap":
            return {"kind": "gap"}
        return None

    # ---- drift observation --------------------------------------------------
    def observe(self, want: dict[str, list[dict]]) -> dict | None:
        """What the gateway can say about what is deployed, and nothing more.

        The discipline here is the whole value of the four-state ladder: a scope
        this runtime cannot read is OMITTED, so `provision` keeps whatever it
        already knew (usually `source: "none"` → `unknown`, "we could not
        look"). Returning an empty map instead would be a CLAIM — "we looked and
        it holds nothing" — which renders as a to-do list of things that are in
        fact live, and is the exact mistake `item_state` exists to prevent.

        `policies` is deliberately never answered: egress rules are
        OpenShell/NemoClaw state recorded host-side, the OpenClaw gateway does
        not own them, and `observed()` has already read them from the registry —
        which works with the container stopped.
        """
        methods = self._client.methods()
        maps: dict[str, dict] = {}
        sources: dict[str, str] = {}

        persona = self._read_file(methods, PERSONA_PATH)
        if persona is not None:
            maps["persona"] = {"IDENTITY.md": _sha(persona)}
            sources["persona"] = "gateway"

        skills = self._skill_digests(methods, want)
        if skills is not None:
            maps["skills"] = skills
            sources["skills"] = "gateway"

        # `servers` is deliberately absent until the gateway can expose a
        # server's FILE TREE. The ladder compares a tree fold of the repo
        # against a tree fold in the sandbox (see AgentRuntime.tree_digests and
        # the incident its docstring records: comparing a tree fold against an
        # entry-point digest made every server read `stale` forever). Knowing
        # only that a server is REGISTERED cannot tell `deployed` from `stale`,
        # and guessing `deployed` is the lie. `unknown` is honest and visible.

        out: dict = {"maps": maps, "sources": sources}
        extras = self._extras(methods)
        if extras:
            out["extras"] = extras
        return out

    def _read_file(self, methods: frozenset[str], name: str) -> str | None:
        """One WORKSPACE file by name, or None.

        `agents.files.get` takes `{agentId, name}` — a bare name inside the
        agent's workspace. It refuses `path` outright ("unexpected property"),
        and it refuses to escape the workspace: `../skills/x/SKILL.md` comes
        back as `unsupported file`. Both verified live.

        The previous version asked for `agents.files.read`, which is not a
        gateway method, with a `path` parameter, which is not a gateway
        parameter. It could only ever return None.
        """
        if "agents.files.get" not in methods:
            return None
        try:
            got = self._client.rpc("agents.files.get",
                                   {"agentId": config.OC_AGENT, "name": name},
                                   timeout=15.0)
        except GatewayError:
            return None
        got = got.get("file") if isinstance(got.get("file"), dict) else got
        for key in ("content", "text", "body"):
            val = got.get(key)
            if isinstance(val, str):
                return val
        return None

    def _skill_digests(self, methods: frozenset[str],
                       want: dict[str, list[dict]]) -> dict | None:
        """One digest per skill the checkout declares, keyed the way `want` is.

        Keyed on `sandbox_id` because the sandbox installs a skill under its
        SKILL.md frontmatter `name:`, which is not the repo directory — the same
        distinction `provision.desired()` already carries for this reason.
        """
        # The gateway cannot answer this one, and saying so is the answer.
        #
        # Skills live in `managedSkillsDir` (/sandbox/.openclaw/skills), OUTSIDE
        # the agent workspace that `agents.files.get` serves — it rejects the
        # path as `unsupported file`. `skills.status` lists what is installed
        # (name, source, filePath) but exposes no CONTENT, and a digest map
        # without content cannot be computed. Nothing else in the 218-method
        # surface returns a skill's bytes.
        #
        # So this returns None, and `provision.observed` leaves the scope at
        # source="none" -> `unknown` -> never `pending`. That is exactly what
        # the four-state ladder is for: an honest "not observable from here"
        # rather than a guessed `deployed`. The CLI path still digests these.
        #
        # It previously gated on `agents.files.read`, an invented method, so it
        # ALSO always returned None — but by accident, reading as though it
        # should have worked.
        return None

    def _extras(self, methods: frozenset[str]) -> dict | None:
        """Plugins and cron jobs: live, undeclared, and not the ladder's business.

        Ava's checkout declares no plugins and no cron jobs — nothing in
        `agent/` renders them — so an item here can never be `stale` or
        `deployed`, only extra. `provision._orphans` is where extras live, and
        `pending` does not count them, so surfacing these cannot make Apply
        appear to have work it cannot do.
        """
        out: dict = {}
        # `plugins.list` is not a method; the surface offers
        # `plugins.uiDescriptors` (-> {ok, descriptors[]}) and
        # `plugins.sessionAction`. Verified live.
        # `tasks.list` is the IN-FLIGHT background-task list, not the scheduler —
        # it answers {tasks:[]} and always looks empty, so a box with real cron
        # jobs reported "no extra cron jobs" in `ava doctor`, a false negative.
        # The scheduler is `cron.list` -> {jobs}. Verified live 2026-08-24.
        for key, method, field in (("plugins", "plugins.uiDescriptors",
                                    "descriptors"),
                                   ("cron", "cron.list", "jobs")):
            if method not in methods:
                out[key] = None      # could not look — never "nothing extra"
                continue
            try:
                got = self._client.rpc(method, {}, timeout=15.0)
            except GatewayError:
                out[key] = None
                continue
            rows = got.get(field) or got.get("items") or []
            out[key] = sorted(
                str(r.get("id") or r.get("name") or r)
                for r in rows if isinstance(r, (dict, str))) if isinstance(rows, list) else None
        return out or None

    # ---- provisioning -------------------------------------------------------
    def provision(self, auto_install: bool = False, scope: str = "all",
                  on_line=None, connector: str | None = None) -> dict:
        # Local import: the package imports THIS module to build the registry,
        # so reaching back up to it has to happen at call time.
        from .. import runtime as _runtime
        local = _runtime.nemoclaw()
        if local.cli:
            # Deploying is `agent/install.sh` pushing bytes through the CLI. The
            # gateway cannot do it, and the sandbox is the same sandbox — so hand
            # the job to the adapter that owns the tool.
            return local.provision(auto_install=auto_install, scope=scope,
                                   on_line=on_line, connector=connector)
        return {"ok": False, "steps": [],
                "error_code": "gateway_cannot_deploy",
                "detail": ("The gateway can observe and control the agent, but "
                           "deploying tools, skills and policies runs "
                           "`agent/install.sh` through the nemoclaw CLI, which "
                           "is not installed on this machine. Install it with "
                           "`ava agent provision --install`."),
                "scope": scope}

    # ---- status -------------------------------------------------------------
    def status(self) -> dict:
        """The SHARED status shape, not a private one.

        `sandbox` is a STRING — the sandbox name — because that is what every
        other runtime returns and what the UI renders directly:
        `AgentRuntimePanel` interpolates it into a template literal and
        `BrainPanel` prints `sandbox "{...}"`. Returning a nested object here
        instead put an object where React expected a child and took the whole
        Agent view down with "Minified React error #31: objects are not valid
        as a React child". The extra facts belong in sibling keys, exactly as
        NemoClawRuntime already publishes `sandbox_model`/`sandbox_provider`.
        """
        st = self._client.status()
        rec = self.registry_record() or {}
        info = self.sandbox_info() or {}
        return {"name": self.name, "available": self.available(),
                # The CLI is not how this runtime talks to the agent, but the
                # panel shows it and provisioning still needs it, so report it
                # the same way rather than leaving a row permanently blank.
                "cli": shutil.which("nemoclaw"),
                "sandbox": config.OC_SANDBOX,
                "sandbox_model": info.get("model"),
                "sandbox_provider": info.get("provider"),
                # A registry entry is what "this sandbox exists" means here;
                # None would render as an unknown we can actually answer.
                "sandbox_exists": bool(rec),
                "agent_version": rec.get("agentVersion"),
                "health": None,
                "gateway": {"phase": st.get("phase"), "why": st.get("why"),
                            "protocol": st.get("protocol"),
                            "url_class": st.get("url_class"),
                            "method_count": len(st.get("methods") or [])},
                "supports_tools": self.supports_tools,
                "supports_cot": self.supports_cot}


# ---- payload shape helpers -------------------------------------------------
# The gateway's payloads are its own; every reader here tolerates both camelCase
# and snake_case and returns None rather than guessing, so a shape change shows
# up as "we could not read it" instead of as a wrong answer.

def _messages_of(payload: dict) -> list[dict]:
    for key in ("messages", "items", "history"):
        got = payload.get(key)
        if isinstance(got, list):
            return [m for m in got if isinstance(m, dict)]
    return []


def _text_of(msg: dict) -> str:
    for key in ("text", "content", "message"):
        got = msg.get(key)
        if isinstance(got, str):
            return got.strip()
        if isinstance(got, list):  # content parts
            parts = [p.get("text") for p in got
                     if isinstance(p, dict) and isinstance(p.get("text"), str)]
            if parts:
                return "\n".join(parts).strip()
    return ""


def _final_text(payload: dict) -> str | None:
    got = _text_of(payload)
    if got:
        return got
    msg = payload.get("message")
    if isinstance(msg, dict):
        return _text_of(msg) or None
    return None


def _tools_of(payload: dict) -> list[str]:
    for key in ("tools", "toolsUsed", "tools_used"):
        got = payload.get(key)
        if isinstance(got, list):
            return [str(t.get("name") if isinstance(t, dict) else t)
                    for t in got if t]
    return []


def _step_of(payload: dict) -> dict | None:
    """One gateway event → one chain-of-thought step in Ava's shape.

    `frontend/src/lib/types.ts` defines CotStep as thinking | text | tool |
    tool_result. A tool call arrives as a `phase` lifecycle — start | update |
    result — under `data`, and this collapses it to at most two steps a reader
    cares about:

        start   → {kind: "tool",        name, id?, args?}
        update  → dropped (partial output; the result frame carries the whole)
        result  → {kind: "tool_result", id?, name, output?, attachments?, is_error?}

    The `tool_result` is FOLDED back into its matching `tool` step by id — in
    `turns._fold_step` for the persisted record and in `chatEvents.foldStep` for
    the live view — so what renders is one enriched tool card per call, never a
    duplicate "Using exec" row. Emitting the two kinds and folding downstream is
    what keeps the live relay and the record identical.
    """
    # `agent` events nest their content one level down, under `data`; `chat`
    # events carry it at the top. Look in both rather than at a guessed shape.
    tf = _tool_frame(payload)
    if tf is not None:
        name = _tool_name(tf) or "tool"
        phase = str(tf.get("phase") or "").lower()
        call_id = _call_id(tf)
        has_result = (tf.get("result") is not None
                      or "isError" in tf or "is_error" in tf)
        if phase == "result" or (phase not in ("start", "update") and has_result):
            output, atts = _tool_result_content(tf.get("result"))
            step: dict = {"kind": "tool_result", "name": name}
            if call_id:
                step["id"] = call_id
            if output:
                step["output"] = output
            if atts:
                step["attachments"] = atts
            if tf.get("isError") or tf.get("is_error"):
                step["is_error"] = True
            return step
        if phase == "update":
            return None
        step = {"kind": "tool", "name": name}
        if call_id:
            step["id"] = call_id
        args = tf.get("args") or tf.get("arguments") or tf.get("input")
        if isinstance(args, (dict, str)):
            step["args"] = args
        return step
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for src in (payload, data):
        # `deltaText` is the chat stream's increment; `delta` the agent
        # stream's. Prefer an increment over the cumulative `text`, or every
        # frame re-renders the whole reply so far.
        for key, kind in (("thinking", "thinking"), ("reasoning", "thinking"),
                          ("deltaText", "text"), ("delta", "text"),
                          ("text", "text")):
            got = src.get(key)
            if isinstance(got, str) and got.strip():
                return {"kind": kind, "text": got.strip()}
    return None


def _tool_frame(payload: dict) -> dict | None:
    """The dict that actually holds a tool call, or None.

    Agent events carry it under `data`; a legacy/top-level shape carries it on
    the payload. Whichever has a tool name wins, and every other field (phase,
    id, result) is then read from that same dict — so a `phase` on `data` is not
    accidentally paired with a `name` on the payload.
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if _tool_name(data):
        return data
    if _tool_name(payload):
        return payload
    return None


def _tool_name(payload: dict) -> str | None:
    for key in ("tool", "toolName", "tool_name", "name"):
        got = payload.get(key)
        if isinstance(got, str) and got.strip():
            return got.strip()
        if isinstance(got, dict) and isinstance(got.get("name"), str):
            return got["name"].strip()
    return None


def _call_id(frame: dict) -> str | None:
    for key in ("toolCallId", "tool_call_id", "callId", "call_id", "id"):
        got = frame.get(key)
        if isinstance(got, str) and got.strip():
            return got.strip()
    return None


def _tool_result_content(result) -> tuple[str, list[dict]]:
    """A tool result's content blocks → (bounded text, media refs).

    OpenClaw strips image bytes out of tool events to `{omitted: true, bytes}`,
    so most tool cards carry text and no playable media; a URL-bearing block
    (a web tool, a connector) still yields a ref. Refs are RAW here — resolved
    to same-origin, browser-fetchable URLs later by `agent_media`.
    """
    text_parts: list[str] = []
    atts: list[dict] = []
    if isinstance(result, str):
        blocks: list = [result]
    elif isinstance(result, list):
        blocks = result
    elif isinstance(result, dict):
        blocks = result.get("content") if isinstance(result.get("content"), list) else [result]
    else:
        blocks = []
    for b in blocks:
        if isinstance(b, str):
            text_parts.append(b)
            continue
        if not isinstance(b, dict):
            continue
        if isinstance(b.get("text"), str):
            text_parts.append(b["text"])
        ref = _media_ref_from_block(b)
        if ref:
            atts.append(ref)
    out = "\n".join(p for p in text_parts if p and p.strip()).strip()
    if len(out) > _TOOL_OUTPUT_MAX:
        out = out[:_TOOL_OUTPUT_MAX] + " …[truncated]"
    return out, atts


def _media_ref_from_block(block: dict) -> dict | None:
    """A content block → a raw media ref {url, mime?, filename?}, or None.

    Skips a block OpenClaw omitted (bytes stripped, no fetchable location) —
    a ref with no URL would only render a broken player.
    """
    if block.get("omitted"):
        return None
    url = None
    for key in ("url", "mediaUrl", "uri", "source", "path", "src"):
        got = block.get(key)
        if isinstance(got, str) and got.strip():
            url = got.strip()
            break
    if not url:
        return None
    ref: dict = {"url": url}
    for key in ("mimeType", "mime", "contentType", "content_type"):
        got = block.get(key)
        if isinstance(got, str) and got.strip():
            ref["mime"] = got.strip()
            break
    for key in ("filename", "name", "title"):
        got = block.get(key)
        if isinstance(got, str) and got.strip():
            ref["filename"] = got.strip()
            break
    return ref


def _final_media(payload: dict) -> list[dict]:
    """Raw media refs a finished reply carries, from every place OpenClaw puts
    one: `mediaUrl`/`mediaUrls` on the payload or its message, media content
    blocks, and `MEDIA: <ref>` lines in the reply text. De-duplicated by URL,
    order preserved."""
    refs: list[dict] = []
    msg = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    for holder in (payload, msg):
        one = holder.get("mediaUrl")
        if isinstance(one, str) and one.strip():
            refs.append({"url": one.strip()})
        many = holder.get("mediaUrls")
        if isinstance(many, list):
            refs.extend({"url": u.strip()} for u in many
                        if isinstance(u, str) and u.strip())
        content = holder.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict):
                    ref = _media_ref_from_block(b)
                    if ref:
                        refs.append(ref)
    text = _text_of(msg) or _text_of(payload)
    for m in _MEDIA_LINE.finditer(text or ""):
        one = m.group(1).strip()
        if one:
            refs.append({"url": one})
    seen: set[str] = set()
    out: list[dict] = []
    for r in refs:
        u = r.get("url")
        if u and u not in seen:
            seen.add(u)
            out.append(r)
    return out


def _strip_media_lines(text: str) -> str:
    """Drop `MEDIA: <ref>` lines from reply prose — they render as media cards,
    not as text to read or speak."""
    if not text:
        return text
    kept = [ln for ln in text.splitlines() if not _MEDIA_LINE.match(ln)]
    return "\n".join(kept).strip()


def _usage_tokens(payload: dict) -> int | None:
    """The prompt/context token count a finished reply reports, if any — the
    honest numerator for the context meter on this runtime (the CLI path had to
    estimate chars/4). Reads the gateway's own `usage`, preferring the
    context-filling count over the total."""
    for holder in (payload, payload.get("message") if isinstance(payload.get("message"), dict) else {}):
        usage = holder.get("usage")
        if not isinstance(usage, dict):
            continue
        for key in ("promptTokens", "prompt_tokens", "inputTokens",
                    "input_tokens", "contextTokens", "totalTokens", "total_tokens"):
            got = usage.get(key)
            if isinstance(got, (int, float)) and got > 0:
                return int(got)
    return None


__all__ = ["OpenClawGatewayRuntime", "GatewayError", "GatewayUnsupported"]
