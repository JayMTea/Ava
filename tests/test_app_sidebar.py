"""The sidebar's two owner-facing facts: the order apps appear in, and whether
each one is ready to use.

ORDER. `ui.order` in a manifest is the AUTHOR's default. The owner's own
arrangement is a separate thing and lives in `ava.yaml` under `ui.app_order`,
for two reasons that rule the manifest out: a built-in connector's manifest is
read-only from the UI (`set_connector_appearance` refuses one outright), so half
the rail could never be dragged; and one drag rewrites every position, which is
one list rather than N independent facts.

The client implements the same sort (`frontend/src/lib/appOrder.ts`) so a drag
lands without a round trip. Both ends must agree, so both are pinned — here and
in `appOrder.test.ts`.

HEALTH. The dot rolls several independent facts into one colour, and the risk is
a confidently wrong green. These tests are mostly about what must NOT be
claimed: not green on a probe we could not read, not red for an app the owner
switched off, not "missing credential" for an app that needs none.
"""
from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from unittest import mock

from ava_bridge import connectors, dashboard


def _write(base, cid, body):
    d = os.path.join(base, cid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "connector.yaml"), "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


def _app(cid, order=None):
    return f"""
        id: {cid}
        label: {cid.title()}
        kind: app
        ui:
          label: {cid.title()}
          section: apps
          embed: iframe
          url: "http://127.0.0.1:9000/"
        {'' if order is None else f'  order: {order}'}
    """


class AppOrderTests(unittest.TestCase):
    """`apps()` honours the owner's arrangement over the manifest default."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.user = os.path.join(self.tmp, "user")
        self.builtin = os.path.join(self.tmp, "builtin")
        os.makedirs(self.user)
        os.makedirs(self.builtin)
        for cid in ("alpha", "beta", "gamma"):
            _write(self.builtin, cid, _app(cid))
        self._p = [
            mock.patch.object(connectors, "BUILTIN_DIR", self.builtin),
            mock.patch.object(connectors, "USER_DIR", self.user),
        ]
        for p in self._p:
            p.start()
        connectors.load(force=True)

    def tearDown(self):
        for p in self._p:
            p.stop()
        connectors.load(force=True)

    def _ids(self, saved):
        with mock.patch.object(connectors.settings, "get",
                               side_effect=lambda k, d=None, **kw: saved
                               if k == connectors.ORDER_KEY else d):
            connectors.load(force=True)
            return [a["id"] for a in connectors.apps()]

    def test_no_arrangement_falls_back_to_manifest_order(self):
        self.assertEqual(self._ids([]), ["alpha", "beta", "gamma"])

    def test_saved_arrangement_wins(self):
        self.assertEqual(self._ids(["gamma", "alpha", "beta"]),
                         ["gamma", "alpha", "beta"])

    def test_an_unplaced_app_lands_at_the_end(self):
        # `beta` was connected after the owner last dragged. It must not be
        # dropped, and must not shoulder into the middle of the arrangement.
        self.assertEqual(self._ids(["gamma", "alpha"]),
                         ["gamma", "alpha", "beta"])

    def test_ids_that_no_longer_exist_are_ignored(self):
        self.assertEqual(self._ids(["gone", "gamma", "alpha", "beta"]),
                         ["gamma", "alpha", "beta"])

    def test_a_duplicated_id_does_not_give_one_app_two_places(self):
        self.assertEqual(self._ids(["gamma", "gamma", "alpha", "beta"]),
                         ["gamma", "alpha", "beta"])

    def test_junk_in_the_stored_value_cannot_blank_the_rail(self):
        # Whatever ends up in ava.yaml, the sidebar still renders every app.
        for junk in ("not-a-list", {"a": 1}, None, [None, 3, ""]):
            with self.subTest(junk=junk):
                self.assertEqual(sorted(self._ids(junk)),
                                 ["alpha", "beta", "gamma"])


class AppVerdictTests(unittest.TestCase):
    """What each colour is allowed to claim."""

    BASE = dict(enabled=True, service="up", auth_env="T", auth_set=True,
                tools_expected=3, tools_deployed=True,
                policy_expected=True, policy_present=True)

    def verdict(self, **over):
        return dashboard._app_verdict({**self.BASE, **over})

    def test_everything_in_place_is_ready(self):
        self.assertEqual(self.verdict(), "ready")

    def test_switched_off_is_never_red(self):
        # Off-by-choice and crashed must not look identical — the same rule
        # ops_services() already follows for a disabled feature.
        self.assertEqual(self.verdict(enabled=False), "off")
        self.assertEqual(self.verdict(enabled=False, service="down"), "off")

    def test_a_probe_that_said_no_outranks_everything_else(self):
        self.assertEqual(self.verdict(service="down", auth_set=False), "down")

    def test_an_unreadable_probe_is_not_green(self):
        # "unknown" means we could not look. Claiming ready would be the
        # confidently-wrong green this whole endpoint exists to avoid.
        self.assertEqual(self.verdict(service="unknown"), "partial")

    def test_no_probe_declared_can_still_be_ready(self):
        # Nothing to be down, and a permanently amber tile is one the owner
        # stops reading. The tooltip says the probe is absent.
        self.assertEqual(self.verdict(service=None), "ready")

    def test_each_missing_piece_downgrades_to_partial(self):
        self.assertEqual(self.verdict(auth_set=False), "partial")
        self.assertEqual(self.verdict(tools_deployed=False), "partial")
        self.assertEqual(self.verdict(policy_present=False), "partial")

    def test_absence_is_only_a_fault_when_something_was_expected(self):
        self.assertEqual(self.verdict(tools_expected=0, tools_deployed=False), "ready")
        self.assertEqual(self.verdict(policy_expected=False, policy_present=False), "ready")


if __name__ == "__main__":
    unittest.main()
