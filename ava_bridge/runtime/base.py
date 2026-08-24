"""AgentRuntime — the seam between Ava and whatever runs her agent.

Ava's agent (tools, memory, skills, sandboxed execution, egress policies) is
provided by a runtime. The default and recommended runtime is **NemoClaw**
(NVIDIA's Apache-2.0 reference stack that runs OpenClaw inside an OpenShell
sandbox — github.com/NVIDIA/NemoClaw). This interface isolates every
runtime-specific detail behind one class so:

  * the ~15 nemoclaw call sites live in ONE place (ava_bridge/runtime/nemoclaw.py),
  * a version bump or a different backend touches only its adapter,
  * and Ava's core talks to `run_turn` / `exec` / `provision`, never a CLI.

`DirectRuntime` is the graceful floor: when no agent runtime is present it talks
to the inference endpoint directly (a working, tool-less assistant). It is the
explicit opt-out, not a silent default — see config `agent.required`.
"""
from __future__ import annotations

import hashlib
import shlex
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field

from .errors import GatewayUnsupported


@dataclass
class RunHandle:
    """A turn that has STARTED, on a runtime that streams it.

    `run_turn()` returns a finished reply because a subprocess had no other
    shape to offer. A gateway answers `chat.send` with an id immediately and
    then streams — so the turn's identity and its outcome are two different
    moments, and the type has to say so or `turns.py` has to guess.

    `session_id` rides along because a reconnect reconciles by re-reading the
    session's history, and the run id alone cannot find it.
    """

    run_id: str
    session_id: str
    idempotency_key: str = ""
    extra: dict = field(default_factory=dict)


class AgentRuntime(ABC):
    name: str = "base"
    # Capabilities the shell uses to decide what to render (e.g. live CoT).
    supports_tools: bool = False
    supports_cot: bool = False

    @abstractmethod
    def available(self) -> bool:
        """True if this runtime can serve a turn right now."""

    @abstractmethod
    def run_turn(self, text: str, session_id: str | None = None,
                 history: list[dict] | None = None) -> tuple[str, list[str]]:
        """Run one Ava turn. Returns (reply_text, tools_used).

        A runtime with its own memory (NemoClaw) uses `session_id` and ignores
        `history`; a stateless runtime (Direct) uses `history` and ignores
        `session_id`.
        """

    # ---- optional capabilities (sane no-op defaults) ------------------------
    def warm(self) -> None:
        """Prime any cold-start cost. No-op by default."""

    def discard_session(self, session_id: str) -> bool:
        """Erase a conversation's runtime-side memory (ghost mode). No-op ok."""
        return True

    def exec(self, inner: str, timeout: int = 20) -> str:
        """Run a shell command inside the runtime sandbox; '' if unsupported."""
        return ""

    def session_file(self, session_id: str) -> str | None:
        """Path to the session transcript inside the sandbox (for live CoT)."""
        return None

    def provision(self, auto_install: bool = False, scope: str = "all",
                  on_line=None, connector: str | None = None) -> dict:
        """Make the runtime ready (install CLI, create sandbox, deploy tools).
        Idempotent. Returns {ok, steps, detail, scope}.

        `scope` is one or more of ava_bridge.provision.SCOPES, comma-separated,
        or "all": changing a persona should not cost a full redeploy of every
        server, skill and policy.

        `connector` narrows a `policies,servers` run to ONE connected app — its
        generated egress policy, and the one server its tools live in. It is
        deliberately NOT a fifth scope: a scope is a domain of the desired
        manifest (`provision.desired()` has exactly four keys) and a connector's
        material is already enumerated as rows inside two of them. A fifth domain
        would double-count it in every pending total.

        `on_line` is an optional `(str) -> None` called with each output line as
        it arrives. A callback rather than a generator because there are three
        implementations — in-process, subprocess line iteration, and an HTTP POST
        — and a callback is the only shape that works identically for all three
        while leaving the return value unchanged for existing callers.
        """
        return {"ok": True, "steps": [], "detail": "no provisioning needed",
                "scope": scope}

    # ---- observation seams (ava_bridge/provision.py) ------------------------
    # Safe no-op defaults on purpose: a runtime that cannot answer yields
    # `source="none"`, which the drift ladder renders as `unknown` — "we could
    # not look" — rather than inventing a verdict or raising.

    def registry_record(self) -> dict | None:
        """This sandbox's entry in the agent runtime's own registry, if any."""
        return None

    def live(self) -> dict:
        """{live, reason} — is the sandbox actually up and reachable right now?

        Distinct from `available()`, which answers "can I serve a turn": a runtime
        can be configured and installed while its container is stopped, and the
        drift report must say so rather than report an empty sandbox.
        """
        return {"live": self.available(), "reason": ""}

    # ---- sandbox probes ----------------------------------------------------
    # Implemented HERE, over `exec()`, rather than on NemoClawRuntime.
    #
    # Every one is a shell command plus parsing, so any runtime that can exec
    # into its sandbox can answer them — and RemoteRuntime can: its `exec`
    # proxies to a shim that wraps the same NemoClawRuntime. Living on the
    # concrete adapter, they were inherited as no-ops instead, so on
    # `agent.runtime: remote` the drift report had no evidence source for
    # persona, servers or skills and reported `unknown` for all three
    # permanently, with nothing anywhere saying why. A "faithful network mirror"
    # that cannot answer three of four scopes is not one.

    def read_file(self, path: str, timeout: int = 20) -> str | None:
        out = self.exec(f"cat -- {shlex.quote(path)}", timeout=timeout)
        return out if out else None

    def digest(self, paths: list[str], timeout: int = 30) -> dict[str, str]:
        """{path: sha256} for files inside the sandbox, in ONE exec.

        `sha256sum` prints `<sum>␣␣<path>`; a missing file goes to stderr (dropped)
        so it simply does not appear in the result — which the drift ladder reads
        as `undeployed`, correctly.
        """
        if not paths:
            return {}
        quoted = " ".join(shlex.quote(p) for p in paths)
        out = self.exec(f"sha256sum -- {quoted} 2>/dev/null", timeout=timeout)
        found: dict[str, str] = {}
        for line in (out or "").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and len(parts[0]) == 64:
                found[parts[1].strip()] = parts[0]
        return found

    def glob_digest(self, pattern: str, timeout: int = 30) -> dict[str, str]:
        """Like digest(), but for a shell glob — for skills, whose in-sandbox
        directory is the SKILL.md frontmatter `name:`, not the repo directory."""
        out = self.exec(f"sha256sum -- {pattern} 2>/dev/null", timeout=timeout)
        found: dict[str, str] = {}
        for line in (out or "").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and len(parts[0]) == 64:
                found[parts[1].strip()] = parts[0]
        return found

    def tree_digests(self, roots: list[str],
                     timeout: int = 30) -> dict[str, str] | None:
        """Fold whole sandbox directories the same way `provision.tree_digest()`
        folds the repo side: sorted `relpath\\0sha256` lines, hashed.

        This is the sandbox half of a promise `tree_digest`'s own docstring
        already made — install.sh extracts `tar czf - -C "$src" .` into a fresh
        directory and swaps that over the destination, so the sandbox copy is a
        byte-exact mirror of the source tree and the two folds are comparable. Without this half, the
        repo's TREE digest was being compared against the sandbox's ENTRY-POINT
        digest, which can never match: every server read `stale` forever, so the
        pending count never cleared and the post-apply assert vetoed every
        successful run.

        One exec for every root. A root that does not exist contributes no
        lines and simply does not appear in the result, which the caller reads
        as absent. Empty output is indistinguishable from "the exec did not
        run", so it yields `None` (unknown) rather than an empty mapping that
        would read as "the sandbox holds nothing".

        `find -type f` tests the link itself, so a symlink inside a server dir
        would be folded on the repo side and skipped here — permanent, loud
        drift rather than a silent wrong answer. Nothing ships one today.
        """
        roots = [r.rstrip("/") for r in roots if r]
        if not roots:
            return {}
        quoted = " ".join(shlex.quote(r) for r in roots)
        try:
            out = self.exec(
                f"find {quoted} -type f -exec sha256sum -- {{}} + 2>/dev/null",
                timeout=timeout)
        except Exception:  # noqa: BLE001 — a failed probe is `unknown`, never `{}`
            return None
        if not (out or "").strip():
            return None

        # Longest root first so nested roots attribute to the deepest one.
        order = sorted(roots, key=len, reverse=True)
        lines: dict[str, list[str]] = {}
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or len(parts[0]) != 64:
                continue
            sha, path = parts[0], parts[1].strip()
            for root in order:
                if path.startswith(root + "/"):
                    lines.setdefault(root, []).append(
                        f"{path[len(root) + 1:]}\0{sha}")
                    break
        return {root: hashlib.sha256(
            "\n".join(sorted(rows)).encode("utf-8")).hexdigest()
            for root, rows in lines.items()}

    def remove_policy(self, preset: str, timeout: int = 60) -> bool:
        """Withdraw one applied egress policy. False when unsupported.

        The counterpart to the `policy-add` that `agent/install.sh` runs. Ava
        only ever added, so a connector's allowance outlived the connector: the
        manifest went, the generated file went, the audit ledger recorded that
        the security posture had changed — and the sandbox carried on permitting
        the routes until someone rebuilt it.
        """
        return False

    # ---- control plane (ava_bridge/runtime/openclaw_gw.py) -----------------
    # Everything below answers "what else can this runtime be asked to do",
    # beyond serving a turn. Defaults refuse rather than pretend: a runtime with
    # no control plane must not look like one whose gateway happened to reject
    # the call, because those have different fixes and the owner is the one who
    # has to tell them apart.

    def capabilities(self) -> list[str]:
        """What this runtime's own service layer supports.

        Promoted from RemoteRuntime with its meaning unchanged: it answers
        "what can this CONTAINER do". `rpc_methods()` answers "what methods does
        the GATEWAY have". Two genuinely different questions — collapsing them
        into one name is how a second answer to one question gets born.
        """
        return []

    def rpc_methods(self) -> frozenset[str]:
        """Control-plane methods this runtime will answer.

        EMPTY MEANS NONE, never "everything". Every caller fails closed on an
        unlisted method, the same way `RemoteRuntime.provision` refuses a scope
        an older shim did not advertise. A terse handshake must not become a
        blanket grant on a token that carries operator.admin.
        """
        return frozenset()

    def rpc(self, method: str, params: dict | None = None, *,
            timeout: float = 30.0, idempotency_key: str | None = None) -> dict:
        """One control-plane call. Raises GatewayError; never returns a partial.

        Synchronous by contract. Every existing caller of this ABC is a worker
        thread or a threadpool'd `def` route, and one seam cannot be half async;
        the async facade lives on the CLIENT (`arpc`), not here.
        """
        raise GatewayUnsupported(self.name)

    def subscribe(self, topics: Sequence[str] | None = None, *,
                  maxlen: int = 1000):
        """Bounded, thread-safe, seq-ordered events.

        `close()` on the result is idempotent and MUST be called — the fan-out
        holds a strong reference until it is.
        """
        raise GatewayUnsupported(self.name)

    def supports_push_turns(self) -> bool:
        """True when `start_run()` works — the turn arrives as events rather
        than as one blocking call.

        `turns.py` branches on THIS, never on `isinstance`. A capability check
        keeps a third runtime from having to be added to a type test somewhere
        far away from itself.
        """
        return False

    def start_run(self, text: str, *, session_id: str, idempotency_key: str,
                  thinking: str | None = None) -> RunHandle:
        """Begin a turn and return at once. Events carry the rest."""
        raise GatewayUnsupported(self.name)

    #: What to CALL this runtime in owner-facing copy. `name` is the config
    #: token (`nemoclaw`, `openclaw_gw`); this is the words. Panels hardcoded
    #: "NemoClaw", which is right for the default runtime and wrong for every
    #: other one — and means a fork running its own runtime has to edit UI
    #: files to stop being told about somebody else's.
    display_name = "Agent runtime"

    def blurb(self) -> str:
        """One sentence: what having this runtime gets you."""
        return "Runs the agent that serves your turns."

    def install_hint(self) -> str | None:
        """What to run when this runtime is configured but not present, or None
        when there is nothing the owner can usefully do from here."""
        return None

    def control_plane(self):
        """The live control-plane client, or None when this runtime has none.

        The ONE way to ask "is there a gateway, and can I talk to it". Callers
        reached into `rt._client` with `getattr(rt, "_client", None)` — testing
        for a PRIVATE ATTRIBUTE's absence to decide whether a runtime has a
        control plane. That silently answers "no gateway" for any adapter that
        happens to name its client something else, and it couples four bridge
        routes to one adapter's internals.

        Returning the client rather than wrapping every call is deliberate: the
        relay forwards arbitrary RPC by design, so a method-per-call facade
        would have to grow a method per gateway feature and would defeat the
        point of a relay.
        """
        return None

    def identity(self) -> dict | None:
        """Who this runtime is talking to, for an operator's own confirmation.

        None when the runtime has nothing to say. Never credentials — an id and
        a name, the things that answer "is this the box I think it is".
        """
        return None

    def pending_approvals(self) -> list[dict]:
        """Commands the runtime has parked for a human, in Ava's row shape.

        Empty by default: a runtime with no approval mechanism has none pending,
        which is the truthful answer and not an error.
        """
        return []

    def resolve_approval(self, approval_id: str, decision: str) -> bool:
        """Answer one parked approval. `decision` is Ava's vocabulary
        (approve | always | deny); translating it is the adapter's job."""
        raise GatewayUnsupported(self.name)

    def is_floor(self) -> bool:
        """Is this the DEGRADED fallback — working chat, but no tools?

        Asked so surfaces can say "tool-less" without knowing the roster of
        runtimes. `rt.name == "direct"` was the old spelling, and
        `supports_tools` is not a substitute: a gateway runtime that has not
        finished connecting reports no tools yet, and reporting it as the floor
        would be wrong in the one moment the owner is watching.
        """
        return False

    def is_local(self) -> bool:
        """Does this runtime run on the SAME machine as the bridge?

        Asked so that surfaces can stop showing CLI and sandbox rows that
        describe a host the operator is not on. It is a property of the adapter
        and belongs to it — the Hub used to answer it with
        `rt is runtime.remote()`, which made the Direct floor (running inside
        this very process) report itself as elsewhere, and which quietly
        required editing the Hub every time a runtime was added.
        """
        return True

    def supports_abort(self) -> bool:
        """True when a run already in flight can be STOPPED.

        Separate from `supports_push_turns()` on purpose: streaming a turn and
        being able to interrupt one are different powers, and a runtime may
        gain either first. `turns.py` branches on this, never on a name.
        """
        return False

    def abort_run(self, session_id: str, run_id: str = "") -> bool:
        """Ask the runtime to stop a run. Returns True if the ask was accepted.

        NOT a promise that the run has ended, and deliberately not a writer of
        the turn's terminal status. The run's own event stream stays the single
        source of truth for how a turn ended — the runtime reports the ending
        with `aborted` set, that arrives as an `error` kind like any other
        ending, and the ONE existing terminal-status writer records it. An
        abort that also wrote the status would race that path and could mark a
        turn stopped that in fact completed a moment earlier.
        """
        raise GatewayUnsupported(self.name)

    def iter_run(self, sub, handle: RunHandle, timeout: float | None = None):
        """A started run's progress, in AVA's vocabulary rather than the
        runtime's own.

        This is the seam's most important translation. `turns.py` consumes four
        kinds and nothing else:

            {"kind": "step",  "step": {kind: thinking|text|tool, ...}}
            {"kind": "final", "text": str, "tools": [str]}
            {"kind": "error", "message": str, "code": str}
            {"kind": "gap"}     events were lost; infer nothing from what arrived

        Keeping the wire format on this side is what lets a rename upstream, or
        a second streaming runtime with entirely different event names, land in
        one adapter instead of in the turn path.

        Exhausting WITHOUT a `final` is how a timeout is reported. It is not an
        exception, because the caller is the one who knows what a partial turn
        is worth — and it already has to handle "the run ended badly" anyway.
        """
        raise GatewayUnsupported(self.name)

    def translate_event(self, topic: str, payload: dict) -> dict | None:
        """One relayed gateway event → Ava's vocabulary, or None if it is not
        turn progress.

        `iter_run` keeps the wire format off the TURN path. This keeps it off
        the BROWSER: the `/ws/gateway` relay carries the gateway's own topics
        for panels that want them, and additionally publishes the translation
        under `ava.run` so the chat client consumes the same four kinds
        `turns.py` does — `step` / `final` / `error` / `gap`.

        Without it the frontend would need a second copy of the event-name table
        in TypeScript, and a rename upstream would have to be found and fixed in
        two languages.
        """
        return None

    def observe(self, want: dict[str, list[dict]]) -> dict | None:
        """`{"maps": {scope: {id: sha256}}, "sources": {scope: str}}`, or None.

        None means "this runtime has no view of its own" and the caller falls
        back to probing over `exec()`. A scope this runtime cannot answer must
        be OMITTED rather than reported empty: `provision.item_state` reads a
        missing source as `unknown` ("we could not look") and an empty map as
        `undeployed` ("we looked and it is gone"), and those are different
        claims about the world.
        """
        return None

    def status(self) -> dict:
        """Rich health for `ava doctor` / the ops dashboard."""
        return {"name": self.name, "available": self.available()}
