"""The demo mock's probe response must carry every field the real one does.

`demo/src/engine/mock.ts` stands in for the bridge while the studio records the
walkthroughs, so a field the mock forgets is not a test-data gap — it is what the
camera sees, and it ships as a video on the docs site.

One was missing. `ava_bridge/hub/connectors.py` returns
`{"ok", "kind": "mcp", "transport": "http"|"stdio", "tools"}` from a successful
MCP probe; the mock returned everything but `transport`. `ConnectorsPanel.tsx`
interpolated it unguarded, so the connect walkthrough renders

    Found 6 tools  via MCP (undefined) — Ava will discover and call these for you.

at 0:38 of `docs/assets/connect-app-tour.mp4`, published. Nothing failed: the mock
answered 200, the panel rendered, the tour recorded, and the only symptom was four
letters on screen. That is the failure mode this guard exists for — a mock is only
wrong in ways nothing throws on.

Both halves were fixed (the mock now sends it; the panel no longer interpolates a
value it does not have), and this pins the mock half. Source text, not an import:
the mock is TypeScript and the point is to compare it against Python.

`demo/` is the maintainer's capture studio and is excluded from the repo, so on a
fork these skip and nothing is checked. That asymmetry is correct and matches
`test_no_owner_identity`'s private-names ratchet: only the person holding the
studio can publish a frame from it, so only there does the guard need teeth.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOCK = ROOT / "demo" / "src" / "engine" / "mock.ts"
BRIDGE = ROOT / "ava_bridge" / "hub" / "connectors.py"


class ProbeShapeTests(unittest.TestCase):
    def setUp(self):
        if not MOCK.is_file():
            self.skipTest("no demo/ in this checkout")
        self.mock = MOCK.read_text(encoding="utf-8")
        self.bridge = BRIDGE.read_text(encoding="utf-8")

    def _bridge_probe_fields(self) -> set:
        """The keys the bridge returns from a successful MCP probe."""
        found = set()
        for m in re.finditer(r'return \{"ok": True, "kind": "mcp",([^}]*)\}',
                             self.bridge):
            found |= set(re.findall(r'"(\w+)":', m.group(1)))
        return found

    def _mock_probe_block(self) -> str:
        """The mock's /connectors/probe handler body.

        The route is a JS regex literal, so the slashes are backslash-escaped —
        `/^\\/api\\/hub\\/connectors\\/probe$/`. Searching for the plain path
        finds nothing, which is how the first version of this guard reported
        "the probe route moved" against an unmoved route.
        """
        m = re.search(r"connectors\\?/probe", self.mock)
        self.assertIsNotNone(m, "the probe route moved in mock.ts")
        return self.mock[m.start():m.start() + 1400]

    def test_the_bridge_still_returns_transport(self):
        """A floor: if the bridge stops sending it, this guard is meaningless
        and should be deleted rather than left passing vacuously."""
        self.assertIn("transport", self._bridge_probe_fields(),
                      "the bridge's MCP probe no longer returns `transport` — "
                      "re-derive what this guard should compare.")

    def test_the_mock_sends_every_field_the_bridge_does(self):
        block = self._mock_probe_block()
        missing = [f for f in sorted(self._bridge_probe_fields())
                   if not re.search(rf"\b{f}\s*:", block)]
        self.assertFalse(missing, (
            f"demo/src/engine/mock.ts's probe response omits {missing}, which "
            "ava_bridge/hub/connectors.py always returns. The mock is what the "
            "studio records, so a missing field renders as `undefined` in a "
            "published video — that is how `MCP (undefined)` reached 0:38 of "
            "connect-app-tour.mp4."))


if __name__ == "__main__":
    unittest.main()
