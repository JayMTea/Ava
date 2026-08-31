"""Every runtime answers the seam's questions, and none of them bluffs.

`ava_bridge/runtime/base.py` is the one interface Ava's core talks to, and
`docs/AGENT_RUNTIME_REFERENCE.md` promises that adding a runtime is a file. That
promise only holds if the DEFAULTS are safe: a new adapter inherits every method
it does not write, so a default that returns something plausible instead of
refusing is a lie the next adapter tells for free.

The rule this file enforces is one rule, applied to each new member:

    a runtime that cannot do a thing must REFUSE, not return an empty success.

`[]` from `rpc_methods()` means "no methods", and every caller fails closed on
it — the same shape as `RemoteRuntime.provision` refusing a scope an older shim
did not advertise, rather than silently widening it into a full run.

House style (tests/test_runtime_gate.py): stdlib unittest, no bridge, no
sandbox, no network.
"""
from __future__ import annotations

import contextlib
import inspect
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-rtcaps-test-"))

from ava_bridge import runtime
from ava_bridge.runtime.base import AgentRuntime, RunHandle
from ava_bridge.runtime.errors import GatewayError, GatewayUnsupported

# Every adapter the registry can select, deduplicated (aliases share instances).
RUNTIMES = sorted({id(r): r for r in runtime._REGISTRY.values()}.values(),
                  key=lambda r: r.name)

# NO NETWORK. This must be started before the partition below, because
# `RemoteRuntime.capabilities()` and `.status()` are live HTTP probes of an agent
# shim on another machine. Without the pin, the partition — and every assertion
# keyed off it — was a fact about whether a service somewhere else happened to
# answer during collection: CI got one parametrisation and a maintainer on the
# tailnet got the opposite, from the same source tree. The file's own docstring
# has always claimed "no network"; this is what makes that true.
# STARTED IN setUpModule, NOT AT IMPORT. A `mock.patch(...).start()` at module
# scope runs during pytest COLLECTION and stays live until this module's
# teardown — which is to say, across every test file that sorts between them.
# It did: it replaced `requests` for `tests/test_remote_gateway_proxy.py` too and
# failed a test there that passes alone. Module-scope patches are collection-time
# global state; keep them inside the fixture that owns them.
_NO_SHIM = mock.patch("ava_bridge.runtime.remote.requests",
                      **{"get.side_effect": OSError("no network in this file"),
                         "post.side_effect": OSError("no network in this file")})


def setUpModule():
    _NO_SHIM.start()
    for rt in RUNTIMES:
        cache = getattr(rt, "_avail_cache", None)
        if isinstance(cache, dict):
            cache.update(ts=0.0, ok=None, caps=[])


def tearDownModule():
    _NO_SHIM.stop()


# The split that matters, and it is NOT "is it connected right now".
#
# `GatewayUnsupported` means "this runtime has no control plane" — a permanent
# fact about the adapter. `GatewayError(agent_down)` means "it has one and it is
# not answering" — a transient fact about the box. Those have different fixes
# (choose a different runtime vs start the gateway), which is the whole reason
# errors.py keeps them as two types, so the guard has to test them separately or
# it quietly permits one to be reported as the other.
#
# THE PARTITION IS `control_plane()`, NOT `capabilities()`, and the difference is
# the whole reason `remote` exists. `capabilities()` answers "what does the far
# side offer RIGHT NOW", which for a cross-host adapter is a fact about someone
# else's uptime; `control_plane()` answers "does this adapter have a control
# plane at all", which is the permanent fact `GatewayUnsupported` is about.
# Keying the refusal contract on the transient one meant a runtime was obliged to
# claim it had no control plane whenever its agent host was briefly unreachable —
# and "choose a different runtime" is the wrong fix for a container that needs
# rebuilding, on a machine the owner has not been told about.
HAS_CONTROL_PLANE = [r for r in RUNTIMES if r.control_plane() is not None]
NO_CONTROL_PLANE = [r for r in RUNTIMES if r.control_plane() is None]

# The third state the binary split does not model: an adapter that HAS a control
# plane whose usefulness depends on a host somewhere else. `direct`/`nemoclaw`
# never have one; `openclaw_gw` always does, even when its socket is down;
# `remote` has one that can be present, absent or merely unreachable — three
# outcomes with three different fixes on two different machines. Such a runtime
# must still REFUSE rather than bluff, and it must refuse with a code that names
# the real fix instead of borrowing `agent_no_gateway`'s.
def conditional_control_plane():
    """Computed lazily, INSIDE a test, because `capabilities()` is a live probe
    for `remote` and must run under the `setUpModule` pin."""
    return [r for r in HAS_CONTROL_PLANE if "gateway.rpc" not in r.capabilities()]


class ContractTests(unittest.TestCase):

    def test_a_conditional_control_plane_refuses_without_bluffing(self):
        """`remote` proxies the gateway through an agent container on another
        host. When that container cannot proxy — too old, unreachable, or its
        token rejected — the adapter must still REFUSE, and it must NOT reuse
        `agent_no_gateway`: that code's copy tells the owner to select
        `agent.runtime: openclaw_gw`, which on a two-host install is both the
        wrong instruction and the wrong machine. The honest answer names the
        agent host and says to rebuild the container there.
        """
        for rt in conditional_control_plane():
            with self.subTest(runtime=rt.name):
                with self.assertRaises(GatewayError) as ctx:
                    rt.rpc("system.info")
                self.assertNotIsInstance(
                    ctx.exception, GatewayUnsupported,
                    "a runtime whose control plane is merely unreachable "
                    "reported having none at all")
                self.assertTrue(ctx.exception.code)
                self.assertNotEqual(ctx.exception.code, "agent_no_gateway")
                self.assertTrue(str(ctx.exception),
                                "the refusal carries no owner-facing sentence")

    def test_a_conditional_control_plane_still_hands_back_a_subscription(self):
        """Same rule as a disconnected gateway: empty, but not dead. A queue that
        can never yield is indistinguishable from a quiet agent."""
        for rt in conditional_control_plane():
            with self.subTest(runtime=rt.name):
                sub = rt.subscribe(["run.step"])
                try:
                    self.assertIsNone(sub.get(timeout=0))
                finally:
                    sub.close()

    def test_the_partition_is_not_a_network_probe(self):
        """The pin at the top of this file is load-bearing, not hygiene: without
        it this file asserted opposite contracts on CI and on a maintainer's box.
        If `remote` ever lands in NO_CONTROL_PLANE again, the refusal tests below
        will demand `GatewayUnsupported` from an adapter that legitimately has a
        control plane, and the failure will look like a bug in the adapter."""
        names = {r.name for r in HAS_CONTROL_PLANE}
        self.assertIn("remote", names,
                      "`remote` has a control plane whether or not its agent "
                      "host answers — the partition must not depend on a probe")
        self.assertTrue(conditional_control_plane(),
                        "nothing exercises the conditional contract")

    def test_the_registry_is_not_empty(self):
        """A vacuous pass is the failure mode every guard in this repo watches
        for — see test_event_vocabulary's own scan-finds-something test."""
        self.assertGreaterEqual(len(RUNTIMES), 4)
        self.assertTrue(NO_CONTROL_PLANE, "nothing to test the refusals against")
        self.assertTrue(HAS_CONTROL_PLANE,
                        "nothing to test the control-plane side against")

    def test_every_runtime_answers_the_render_flags_with_a_bool(self):
        """`turns.py:318` branches on supports_tools and the chat shell renders
        live chain-of-thought from supports_cot. A property that raises, or
        answers None, turns a rendering decision into an exception on the turn
        path."""
        for rt in RUNTIMES:
            with self.subTest(runtime=rt.name):
                self.assertIsInstance(rt.supports_tools, bool)
                self.assertIsInstance(rt.supports_cot, bool)

    def test_an_empty_method_list_means_none_and_refuses(self):
        for rt in NO_CONTROL_PLANE:
            with self.subTest(runtime=rt.name):
                self.assertEqual(rt.rpc_methods(), frozenset())
                with self.assertRaises(GatewayUnsupported):
                    rt.rpc("system.info")

    def test_a_control_plane_that_is_merely_down_says_so_differently(self):
        """The distinction the two error types exist to preserve: a runtime that
        HAS a gateway and cannot reach it must not report the same thing as one
        that has no gateway at all. One is fixed by starting a service, the
        other by choosing a different runtime."""
        from ava_bridge.runtime import openclaw_gw_client as gwc

        # Point the client at a dead port rather than whatever the ambient
        # ava.yaml names. This test used to pass only because no gateway was
        # running on this box; once one was, it connected for real and reported
        # `gateway_rpc_failed`. A contract test must not depend on a service
        # being absent.
        for rt in HAS_CONTROL_PLANE:
            with self.subTest(runtime=rt.name):
                # Patch the CONFIG, not `_default_url`: the process-wide
                # client binds that function at construction, so replacing the
                # module attribute afterwards is too late. `_default_url` reads
                # the config value on every call, so this one does land.
                with mock.patch.object(gwc, "_READY_WAIT_S", 0.01), \
                        mock.patch.object(gwc.config, "AGENT_GATEWAY_URL",
                                          "ws://127.0.0.1:1/"):
                    with self.assertRaises(GatewayError) as ctx:
                        rt.rpc("system.info")
                self.assertNotIsInstance(
                    ctx.exception, GatewayUnsupported,
                    "a disconnected gateway is not an absent one")
                self.assertEqual(ctx.exception.code, "agent_down")
                self.assertTrue(ctx.exception.retryable)

    def test_a_runtime_that_answers_turns_can_name_its_model(self):
        """`models.effective_brain()` asks the ACTIVE runtime what it thinks
        with, through `getattr(rt, "sandbox_info", None)`. A runtime that
        answers turns but lacks that method resolves to an EMPTY model id, and
        the branch returns source="agent" with nothing in it — so /api/health,
        the chat header, Setup → Agent and the hardware panel all announce "No
        model is configured, so there is nothing to answer with" while turns are
        being answered perfectly well.

        The resolver excuses an empty id as self-correcting, "the name appears
        when the background refresh lands". That is only true of a runtime that
        HAS a refresh. Observed on openclaw_gw, where empty was permanent.
        """
        for rt in RUNTIMES:
            if rt.name in ("direct", "none"):
                continue          # the Direct floor is the router's own branch
            with self.subTest(runtime=rt.name):
                got = getattr(rt, "sandbox_info", None)
                self.assertTrue(
                    callable(got),
                    f"{rt.name} answers turns but cannot name its model, so "
                    f"effective_brain() reports 'agent' with an empty model id")

    # Keys the UI renders DIRECTLY as text. `AgentRuntimePanel` interpolates
    # `st.sandbox` into a template literal and `BrainPanel` prints
    # sandbox "{agentBrain.sandbox}" — neither stringifies first.
    STATUS_SCALARS = ("name", "cli", "sandbox", "sandbox_model",
                      "sandbox_provider", "agent_version")

    def test_every_runtime_agrees_on_the_shape_of_status(self):
        """A runtime that returns an object where the others return a string
        puts an object where React expects a child, and the whole view dies with
        "Minified React error #31: objects are not valid as a React child" — a
        blank screen with a stack trace, not a bad-looking row.

        Observed on openclaw_gw, whose status() returned
        {"sandbox": {"name": ..., "model": ..., "agent_version": ...}} while
        every other runtime returned the sandbox NAME there.
        """
        for rt in RUNTIMES:
            with self.subTest(runtime=rt.name):
                # Neutralise the shell-outs some runtimes make on this path;
                # this test is about the SHAPE, and a `nemoclaw` probe here
                # would add 30s timeouts to the suite for no extra coverage.
                patches = []
                for slow in ("_sandbox_exists", "_doctor"):
                    if hasattr(rt, slow):
                        patches.append(mock.patch.object(
                            rt, slow, lambda *a, **k: None))
                with contextlib.ExitStack() as stack:
                    for pa in patches:
                        stack.enter_context(pa)
                    st = rt.status()
                self.assertIsInstance(st, dict, "status() must be a dict")
                for key in self.STATUS_SCALARS:
                    if key not in st or st[key] is None:
                        continue      # absent or unknown is fine; wrong is not
                    self.assertIsInstance(
                        st[key], str,
                        f"{rt.name}.status()[{key!r}] is "
                        f"{type(st[key]).__name__}, but the UI renders it as a "
                        f"React child — an object there blanks the view")

    def test_a_runtime_that_claims_push_turns_implements_them(self):
        """`supports_push_turns()` is what turns.py branches on. A runtime that
        says True and then falls through to the ABC's refusal strands the turn
        in `running` with nothing to move it on."""
        for rt in RUNTIMES:
            with self.subTest(runtime=rt.name):
                if rt.supports_push_turns():
                    self.assertIsNot(type(rt).start_run, AgentRuntime.start_run,
                                     "claims push turns without implementing "
                                     "start_run")

    def test_a_runtime_with_no_control_plane_refuses_to_start_a_run(self):
        for rt in NO_CONTROL_PLANE:
            with self.subTest(runtime=rt.name):
                self.assertFalse(rt.supports_push_turns())
                with self.assertRaises(GatewayUnsupported):
                    rt.start_run("hi", session_id="s", idempotency_key="k")

    def test_subscribe_refuses_where_there_is_nothing_to_subscribe_to(self):
        """A queue that can never yield is indistinguishable from a quiet agent.

        A gateway runtime that is merely DISCONNECTED is a different case and is
        allowed to hand back a live subscription: the socket comes back, and a
        long-lived UI subscription that survives a reconnect is the point.
        """
        for rt in NO_CONTROL_PLANE:
            with self.subTest(runtime=rt.name):
                with self.assertRaises(GatewayUnsupported):
                    rt.subscribe()

    def test_a_disconnected_gateway_still_hands_back_a_live_subscription(self):
        for rt in HAS_CONTROL_PLANE:
            with self.subTest(runtime=rt.name):
                sub = rt.subscribe(["run.step"])
                try:
                    self.assertIsNone(sub.get(timeout=0),
                                      "empty, but not dead — it must not raise")
                finally:
                    sub.close()

    def test_observe_returns_none_not_an_empty_map(self):
        """`provision.item_state` reads a missing source as `unknown` ("we could
        not look") and an empty map as `undeployed` ("we looked and it is
        gone"). Those are different claims, and only one of them is true for a
        runtime with no view of its own."""
        for rt in RUNTIMES:
            with self.subTest(runtime=rt.name):
                got = rt.observe({})
                self.assertTrue(got is None or isinstance(got, dict))
                if got is not None:
                    self.assertIn("maps", got)
                    self.assertIn("sources", got)

    def test_capabilities_and_rpc_methods_stay_two_questions(self):
        """One answers "what can this container do", the other "what does the
        gateway offer". A runtime that aliases one to the other has collapsed
        two questions into one answer, and the next reader cannot tell which
        was meant."""
        for rt in RUNTIMES:
            with self.subTest(runtime=rt.name):
                self.assertIsInstance(rt.capabilities(), list)
        self.assertIsNot(AgentRuntime.capabilities, AgentRuntime.rpc_methods)

    def test_the_refusal_carries_a_code_the_frontend_can_route(self):
        """`fixes.ts` resolves a fix link by PATTERN, so a coded refusal gets the
        owner a working link with no frontend change."""
        with self.assertRaises(GatewayUnsupported) as ctx:
            AgentRuntime.rpc(runtime.direct(), "system.info")
        self.assertEqual(ctx.exception.code, "agent_no_gateway")
        self.assertIn("agent.runtime", str(ctx.exception))


class RunHandleTests(unittest.TestCase):

    def test_a_started_run_carries_what_a_reconnect_needs(self):
        """A reconnect reconciles by re-reading the session's history; the run
        id alone cannot find it."""
        h = RunHandle(run_id="r1", session_id="s1", idempotency_key="turn:t1")
        self.assertEqual((h.run_id, h.session_id), ("r1", "s1"))
        self.assertEqual(h.extra, {})

    def test_start_run_is_declared_to_return_one(self):
        sig = inspect.signature(AgentRuntime.start_run)
        self.assertEqual(sig.return_annotation, "RunHandle")
        for name in ("session_id", "idempotency_key"):
            self.assertEqual(sig.parameters[name].kind,
                             inspect.Parameter.KEYWORD_ONLY,
                             f"{name} must be keyword-only so a call site "
                             f"cannot silently swap it with `text`")


if __name__ == "__main__":
    unittest.main()
