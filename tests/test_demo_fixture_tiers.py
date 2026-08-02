"""A demo fixture may not claim a consent tier the shipped code contradicts.

`demo/fixtures/*.json` drive the mocked SPA that produces the screenshots in
`docs/assets/` and the frames in the tour videos. So a wrong number in a fixture
is not a test-data problem — it is published, in a picture, as a claim about how
Ava behaves.

One was. `hub-approvals.json` staged an approval banner for a real connector's
logging tool, captioned:

    · physical action — moves something in the real world; asks every time

Measured against that connector's manifest, three things in the banner were
false. The manifest mapped the tool's whole `log_*` family to `write`, so
`action_access()` returned **write** — a tier that is grantable, does not say
"asks every time", and is not physical. The banner also renamed the app to a
generic category word the manifest's `label` does not use. And the screenshot
was published in docs/CONNECT_YOUR_APPS.md, so the docs showed Ava calling a
routine logging call a real-world actuation.

The fix was to stage a tool that genuinely IS physical — one whose manifest
declares the `physical` tier and adds `confirm:`. The demo keeps the dramatic
beat and it becomes true.

(The apps in that story were demo-only and no longer ship, so this note
describes the SHAPE of the bug rather than naming them —
tests/test_no_private_apps_shipped.py is what keeps those names out of tracked
files. The checks below never needed the names: they read whatever a fixture
happens to reference and measure it against whatever `connectors.load()` finds,
so they kept working unchanged.)

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

    def _staged(self):
        """Every `<cid>.<tool>` a fixture stages WITH an access tier.

        Not filtered by what is installed — that filtering belongs to the two
        checks below, which can only measure a connector they can load. Keeping
        it out of here is what lets the floor test stay meaningful: see its
        docstring.
        """
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
                if not node.get("access"):
                    continue
                cid, _, tool = action.partition(".")
                out.append((path.name, cid, tool, node["access"],
                            node.get("connector")))
        return out

    def _claims(self):
        """The staged tiers we can actually measure — those whose connector is
        installed on this box. A fixture may legitimately stage a hypothetical
        app, and one it stages may simply not be installed here."""
        return [c for c in self._staged() if c[1] in self.known]

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
        """A floor, so a rename of the fixture keys cannot make this vacuous.

        Measured over what the fixtures STAGE, not over what this box can load.
        It used to require a staged tool whose connector was installed, which
        tied a key-shape guard to the machine's connector list: once the demo
        apps stopped shipping and the approval fixture was re-staged on one this
        box does not have, the floor failed while the fixtures were perfectly
        well-formed. The two checks above are the ones that need a loadable
        connector, and they already filter for it.
        """
        self.assertTrue(self._staged(),
                        "no fixture stages a `<app>.<tool>` with an access tier "
                        "— did demo/fixtures move, or the key names change?")


if __name__ == "__main__":
    unittest.main()
