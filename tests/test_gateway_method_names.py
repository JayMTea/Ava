"""Every gateway method we call must be one the gateway actually offers.

The Agent tab was written from prose docs, and five of the method names in it
were fiction: `audit.activity.list`, `agents.files.read`, `agents.files.write`,
`plugins.list`, `system.diagnostics.stability`. The client fails CLOSED on an
unadvertised method, so each one surfaced as a red "this gateway does not offer
`x`" — but only in whichever panel the owner happened to open, one screen at a
time, days apart.

Tests could not catch it because `qa/fakes/fake_gateway.py` advertised the same
invented names: a fake built from the same assumption as the caller agrees with
the caller. It now serves `qa/fakes/gateway-methods.txt`, a capture from a live
gateway, and this guard holds the source to that same list.

Scope: a string is only checked when its FIRST SEGMENT is a real gateway
namespace. That is what keeps css classes, filenames and Ava's own event topics
out of it while still catching `audit.activity.list` sitting next to `audit.list`.

House style is tests/test_icon_sizing.py — a static scan, no build, no browser.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURE = os.path.join(ROOT, "qa", "fakes", "gateway-methods.txt")

# Where gateway method names are spelled.
SUBJECTS = (
    os.path.join("frontend", "src", "lib", "agentApi.ts"),
    os.path.join("ava_bridge", "runtime", "openclaw_gw.py"),
)

# Strings that LOOK like methods but are not RPC calls, with the reason.
ALLOW = {
    # Ava's own capability vocabulary, returned by AgentRuntime.capabilities().
    "gateway.rpc", "gateway.events", "gateway.push_turns",
    # Gateway CONFIG KEYS, refused by gateway_api's deny-list — not methods.
    "gateway.controlUi.dangerouslyDisableDeviceAuth",
    "gateway.controlUi.allowInsecureAuth",
}

DOTTED = re.compile(r"['\"]([a-z][a-zA-Z0-9]*(?:\.[a-zA-Z0-9]+)+)['\"]")


def _live() -> set[str]:
    with open(CAPTURE, encoding="utf-8") as f:
        return {ln.strip() for ln in f
                if ln.strip() and not ln.startswith("#")}


class MethodNameTests(unittest.TestCase):

    def test_the_capture_is_present_and_substantial(self):
        """A missing or truncated capture would make every test below pass
        vacuously, which is the one failure mode a guard must not have."""
        live = _live()
        self.assertGreater(len(live), 150,
                           "the captured method surface looks truncated")
        self.assertIn("chat.send", live, "the capture is not a gateway surface")

    def test_no_source_file_calls_a_method_the_gateway_does_not_offer(self):
        live = _live()
        prefixes = {m.split(".")[0] for m in live}
        bad: list[str] = []
        for rel in SUBJECTS:
            path = os.path.join(ROOT, rel)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                for name in set(DOTTED.findall(f.read())):
                    if name in ALLOW or name in live:
                        continue
                    if name.split(".")[0] in prefixes:
                        bad.append(f"{rel}: {name}")
        self.assertFalse(
            sorted(bad),
            "these are not methods this gateway offers — the client fails "
            "closed on them, so each is a dead panel:\n  "
            + "\n  ".join(sorted(bad)))

    def test_the_fake_gateway_serves_the_capture(self):
        """The fake must not be free to agree with a caller's mistake."""
        path = os.path.join(ROOT, "qa", "fakes", "fake_gateway.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("gateway-methods.txt", src,
                      "fake_gateway must read the captured surface, not a "
                      "hand-written list that can drift toward the caller")
        self.assertNotIn('"audit.activity.list"', src,
                         "an invented method is being advertised again")


# ---------------------------------------------------------------------------


class WholeTreeMethodTests(unittest.TestCase):
    """The same check as above, but everywhere — anchored on CALL SITES.

    `SUBJECTS` is two files, chosen because that is where gateway methods were
    *supposed* to be spelled. Every invented name found so far was in one of
    them, which proves the check works and says nothing about the rest of the
    tree. A method name that drifts into a panel, a hook or a CLI path is
    exactly as dead and would not be looked at here.

    A whole-tree scan for dotted strings is useless — css classes, filenames,
    config keys and Ava's own event kinds all look the same. So this anchors on
    the CALL, not the string: the name must appear as the first argument to
    something that performs an RPC.
    """

    #: `call<T>('x.y'`, `.rpc('x.y'`, `run('x.y'`, `client.rpc("x.y"`.
    CALLSITE = re.compile(
        r"(?:\b(?:call|run|rpc|_rpc)\s*(?:<[^>()]*>)?\s*\(\s*)"
        r"['\"]([a-z][a-zA-Z0-9]*(?:\.[a-zA-Z0-9]+)+)['\"]")

    def _sources(self):
        import subprocess
        out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        for rel in out.split():
            if not rel.endswith((".py", ".ts", ".tsx")):
                continue
            if ".test." in rel or rel.startswith(("tests/", "qa/")):
                continue
            yield rel

    def test_every_rpc_call_site_names_a_real_method(self):
        live = _live()
        prefixes = {m.split(".")[0] for m in live}
        bad, checked = [], 0
        for rel in self._sources():
            try:
                with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                    body = f.read()
            except FileNotFoundError:
                self.fail(f"{rel} is tracked by git but missing from disk — "
                          "stage the deletion so the scan and the tree agree")
            for m in self.CALLSITE.finditer(body):
                name = m.group(1)
                if name.split(".")[0] not in prefixes:
                    continue          # not a gateway namespace at all
                checked += 1
                if name in ALLOW or name in live:
                    continue
                line = body[:m.start()].count("\n") + 1
                bad.append(f"{rel}:{line} calls {name}")
        self.assertGreater(
            checked, 10,
            "found almost no rpc call sites — the pattern has drifted from the "
            "code and this guard is passing vacuously")
        self.assertEqual(
            bad, [],
            "an RPC call site names a method this gateway does not offer. The "
            "client fails closed, so each one is a dead surface:\n  "
            + "\n  ".join(bad))


class SeamConfinementTests(unittest.TestCase):
    """Gateway RPC happens in the SEAM, not scattered through the app.

    Widening the scan above tree-wide found no new fiction — every gateway call
    site in the whole repo is in one of two files. That is not luck, it is the
    wrapper's central design claim: `agentApi.ts` is the one place the frontend
    names a gateway method, and `openclaw_gw.py` is the one place the backend
    does. It is worth asserting rather than rediscovering, because the day an
    RPC appears in a panel is the day an upstream rename starts breaking screens
    instead of one adapter.
    """

    SEAM = {
        os.path.join("frontend", "src", "lib", "agentApi.ts"),
        os.path.join("ava_bridge", "runtime", "openclaw_gw.py"),
        # The relay forwards whatever the client asks for; it names methods only
        # to POLICE them (the deny-list), which is the opposite of calling one.
        os.path.join("ava_bridge", "gateway_api.py"),
    }

    def test_gateway_rpc_stays_inside_the_seam(self):
        live = _live()
        prefixes = {m.split(".")[0] for m in live}
        outside = []
        t = WholeTreeMethodTests("test_every_rpc_call_site_names_a_real_method")
        for rel in t._sources():
            if rel.replace("/", os.sep) in self.SEAM:
                continue
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                body = f.read()
            for m in t.CALLSITE.finditer(body):
                if m.group(1).split(".")[0] in prefixes:
                    line = body[:m.start()].count("\n") + 1
                    outside.append(f"{rel}:{line} calls {m.group(1)}")
        self.assertEqual(
            outside, [],
            "a gateway method is called from outside the seam. Route it "
            "through agentApi.ts (frontend) or openclaw_gw.py (backend) so an "
            "upstream rename lands in one adapter instead of across the "
            "app:\n  " + "\n  ".join(outside))


class TopicNameTests(unittest.TestCase):
    """Every topic we SUBSCRIBE to must be one that is actually emitted.

    Same bug class as the method names above, and it hid longer because a
    subscription to a topic nobody sends is SILENT: no red banner, no error —
    the panel just never updates. `AgentView` subscribed to `session.update`,
    which is not in the gateway's vocabulary, so the session list only ever
    refreshed on mount and looked merely "a bit stale" rather than broken.

    The whole live vocabulary is four names (verified against the running
    gateway, OpenClaw 2026.7.1): `agent`, `chat`, `health`, `tick`. State is
    carried in the PAYLOAD (`stream`, `state`), never as a topic suffix — so a
    dotted `x.y` topic is the tell for an invented one. Ava's own synthetic
    frames are namespaced `ava.*` and are emitted by `gatewayClient.ts`.
    """

    # Emitted BY the gateway.
    LIVE_TOPICS = {"agent", "chat", "health", "tick"}
    # Synthesised by Ava's own client and bridge, for Ava's own subscribers.
    # `ava.run` is relayed by gateway_api.py; `ava.gateway.gap` by gatewayClient.
    OWN_TOPICS = {"ava.gateway.gap", "ava.run"}

    # Topics we KNOW are unverified, each with the reason it could not be
    # captured. EMPTY, and the ratchet below keeps it that way: the one entry
    # this ledger ever held was `terminal.output`, which nothing emitted, and
    # it was retired by deleting the subscription rather than by exempting it.
    UNVERIFIED: dict[str, str] = {}

    # `useGatewaySubscription('x', …)` / `client.subscribe('x', …)`.
    SUB = re.compile(
        r"(?:useGatewaySubscription|\.subscribe)\(\s*['\"]([^'\"]+)['\"]")

    def _sources(self):
        import subprocess
        out = subprocess.run(
            ["git", "ls-files", "frontend/src"],
            cwd=ROOT, capture_output=True, text=True, check=True).stdout
        for rel in out.split():
            # `.test.ts` is excluded on purpose: a client unit test subscribes
            # to synthetic topics ('t', 'other') to exercise DISPATCH, and those
            # are not claims about the gateway's vocabulary.
            if rel.endswith((".ts", ".tsx")) and ".test." not in rel:
                yield rel

    def test_every_subscribed_topic_is_one_that_is_emitted(self):
        allowed = self.LIVE_TOPICS | self.OWN_TOPICS | set(self.UNVERIFIED)
        bad = []
        checked = 0
        for rel in self._sources():
            try:
                with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                    body = f.read()
            except FileNotFoundError:
                # Tracked but not on disk: a deletion that was not staged.
                # Say that, rather than dying with a traceback that looks like
                # this guard is broken.
                self.fail(f"{rel} is tracked by git but missing from disk — "
                          "stage the deletion (git rm) so the scan and the "
                          "tree agree")
            for m in self.SUB.finditer(body):
                topic = m.group(1)
                checked += 1
                if topic not in allowed:
                    line = body[:m.start()].count("\n") + 1
                    bad.append(f"{rel}:{line} subscribes to '{topic}'")
        self.assertGreater(
            checked, 0,
            "found no subscriptions at all — the regex has drifted from the "
            "call sites and this guard is passing vacuously")
        self.assertEqual(
            bad, [],
            "subscribed to a topic the gateway never emits — a silent dead "
            "feed, not an error. The vocabulary is agent|chat|health|tick "
            "(state rides in the payload, not the topic) plus Ava's own "
            "ava.* frames:\n  " + "\n  ".join(bad))

    def test_each_synthetic_topic_agrees_with_the_file_that_emits_it(self):
        """Ava's own topics are spelled in two files that never import each
        other — the emitter and every subscriber — so the agreement has to be
        asserted. They have DIFFERENT emitters and even different languages:
        the gap is synthesised client-side, the run frames are relayed by the
        bridge, and pinning both to one file is how a rename goes unnoticed."""
        emitters = {
            "ava.gateway.gap": os.path.join(
                "frontend", "src", "lib", "gatewayClient.ts"),
            "ava.run": os.path.join("ava_bridge", "gateway_api.py"),
        }
        self.assertEqual(
            set(emitters), self.OWN_TOPICS,
            "a synthetic topic was added without naming the file that emits it")
        for topic, rel in emitters.items():
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                body = f.read()
            self.assertIn(
                f'"{topic}"', body.replace("'", '"'),
                f"{topic} is subscribed to but {rel} no longer emits it")

    def test_the_unverified_ledger_states_a_reason_for_every_entry(self):
        """An allowlist without reasons decays into a place to hide fiction."""
        for topic, why in self.UNVERIFIED.items():
            self.assertGreater(
                len(why), 80,
                f"{topic} is exempted without explaining what blocks capture")

    def test_the_unverified_ledger_does_not_grow_silently(self):
        """A ratchet. Capturing a topic REMOVES an entry; nothing may be added
        without a live-probe blocker written down beside it."""
        self.assertEqual(
            self.UNVERIFIED, {},
            "an unverified topic was added — capture it against a live "
            "gateway instead, which is the only thing that has ever caught "
            "one of these")


if __name__ == "__main__":
    unittest.main()
