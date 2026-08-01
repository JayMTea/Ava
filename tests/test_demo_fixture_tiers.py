"""A demo fixture may not claim a consent tier the shipped code contradicts.

`demo/fixtures/*.json` drive the mocked SPA that produces the screenshots in
`docs/assets/` and the frames in the tour videos. So a wrong number in a fixture
is not a test-data problem — it is published, in a picture, as a claim about how
Ava behaves.

One was. `hub-approvals.json` staged an approval banner reading:

    Ava wants to run stridewell.log_workout on Fitness
    · physical action — moves something in the real world; asks every time

Measured against the shipped connector, three things in that sentence are false:
`examples/stridewell/connector.yaml` declares `log_*: write`, so
`action_access("stridewell", "log_workout")` returns **write** — a tier that is
grantable, does not say "asks every time", and is not physical. The connector's
label is `Stridewell`, not `Fitness`. And the screenshot was published in
docs/CONNECT_YOUR_APPS.md, so the docs showed Ava calling a fitness log a
real-world actuation.

The fix was to stage a tool that genuinely IS physical —
`hearthwire.unlock_front_door`, where the manifest declares `unlock_*: physical`
and adds `confirm:`. The demo keeps the dramatic beat and it becomes true.

A fixture is free to invent a hypothetical app (`hub-grants.json` does, with
`log_meal` / `start_treadmill`, belonging to no shipped connector). What it may
not do is name a REAL connector and disagree with it.

`demo/` is the maintainer's capture studio and is excluded from the repo, so on a
fork these skip and nothing is checked. That asymmetry is correct and matches
`test_no_owner_identity`'s private-names ratchet: only the person holding the
studio can publish a frame from it, so only there does the guard need teeth.
"""
import json
import pathlib
import unittest

from ava_bridge import connectors

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "demo" / "fixtures"


def _walk(node):
    """Every dict in a nested JSON document."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


class DemoFixtureTierTests(unittest.TestCase):
    def setUp(self):
        if not FIXTURES.is_dir():
            self.skipTest("no demo/fixtures in this checkout")
        self.known = {m["id"]: m for m in connectors.load()}
        self.labels = {a["id"]: a["label"] for a in connectors.apps()}

    def _claims(self):
        """[(file, cid, tool, declared_access, declared_connector_label)]."""
        out = []
        for path in sorted(FIXTURES.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for node in _walk(doc):
                action = node.get("action") or node.get("tool")
                if not isinstance(action, str) or "." not in action:
                    continue
                cid, _, tool = action.partition(".")
                if cid not in self.known or not node.get("access"):
                    continue
                out.append((path.name, cid, tool, node["access"],
                            node.get("connector")))
        return out

    def test_no_fixture_claims_a_tier_the_connector_contradicts(self):
        wrong = []
        for fname, cid, tool, claimed, _label in self._claims():
            real = connectors.action_access(cid, tool)
            if claimed != real:
                wrong.append(f"{fname}: {cid}.{tool} claims '{claimed}', "
                             f"connectors.action_access says '{real}'")
        self.assertFalse(wrong, (
            "these demo fixtures stage a consent tier the shipped connector "
            f"does not produce: {wrong}. They drive the published screenshots, "
            "so the wrong one becomes a picture of Ava doing something it does "
            "not do. Either fix the fixture or stage a tool that really has "
            "that tier."))

    def test_no_fixture_renames_a_real_connector(self):
        wrong = []
        for fname, cid, tool, _claimed, label in self._claims():
            if label and label != self.labels.get(cid, label):
                wrong.append(f"{fname}: {cid}.{tool} shows the app as "
                             f"{label!r}, but its label is "
                             f"{self.labels.get(cid)!r}")
        self.assertFalse(wrong, "\n".join(wrong))

    def test_the_scan_finds_something(self):
        """A floor, so a rename of the fixture keys cannot make this vacuous."""
        self.assertTrue(self._claims(),
                        "no fixture names a shipped connector's tool with an "
                        "access tier — did demo/fixtures move, or the key "
                        "names change?")


if __name__ == "__main__":
    unittest.main()
