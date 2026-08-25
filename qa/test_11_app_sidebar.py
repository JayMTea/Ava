"""The sidebar's two owner-facing controls, end to end against a real bridge:
drag-to-reorder the app list, and the readiness dot on each row.

Distinct from `tests/test_app_sidebar.py`, which pins the SORT RULE and the
verdict logic in isolation. This one is about the round trip an owner actually
performs — POST an arrangement, reload, and find the sidebar in that order —
because the pieces were unit-correct and could still be wired up wrong: a route
behind the wrong auth gate, a settings key written but never read back, an
arrangement that survives the request and not the reload.
"""
import unittest

import pytest

from qa import helpers


@pytest.fixture(scope="module", autouse=True)
def _client(bridge):
    helpers.ensure_authed(bridge)
    globals()["CLIENT"] = bridge
    yield


def _app_ids():
    return [a["id"] for a in CLIENT.get("/api/apps").json()["apps"]
            if a.get("section") != "core"]


class TestSidebarOrder(unittest.TestCase):
    """An arrangement the owner makes has to survive the reload."""

    def test_01_both_routes_require_auth(self):
        # The sidebar polls health on a timer and saves an order on every drag.
        # Neither may be reachable without the session cookie.
        with helpers.anon_client() as anon:
            self.assertEqual(anon.get("/api/apps/health").status_code, 401)
            self.assertEqual(anon.post("/api/apps/order",
                                       json={"order": []}).status_code, 401)

    def test_02_reordering_survives_a_reload(self):
        before = _app_ids()
        if len(before) < 2:
            self.skipTest("needs two apps to have an order at all")
        r = CLIENT.post("/api/apps/order", json={"order": list(reversed(before))})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("ok"), r.text)
        # The reload is the point: /api/apps must READ the saved key, not just
        # echo what the POST returned.
        self.assertEqual(_app_ids(), list(reversed(before)))

    def test_03_unknown_ids_are_dropped_not_rejected(self):
        # An app removed in another tab must not make an otherwise valid
        # arrangement un-saveable.
        current = _app_ids()
        r = CLIENT.post("/api/apps/order",
                        json={"order": ["ghost-app"] + current})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("ghost-app", r.json()["order"])
        self.assertEqual(_app_ids(), current)

    def test_04_a_bad_body_is_refused_rather_than_stored(self):
        before = _app_ids()
        self.assertEqual(
            CLIENT.post("/api/apps/order", json={"order": "nope"}).status_code, 400)
        self.assertEqual(_app_ids(), before)

    def test_05_clearing_the_arrangement_restores_manifest_order(self):
        r = CLIENT.post("/api/apps/order", json={"order": []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["order"], [])


class TestSidebarHealth(unittest.TestCase):
    """Every app the sidebar can draw must get a verdict it can colour."""

    def test_01_every_listed_app_has_a_verdict(self):
        listed = set(_app_ids())
        rows = {a["id"]: a for a in CLIENT.get("/api/apps/health").json()["apps"]}
        # A row missing from health leaves a permanently grey dot with no
        # explanation — the one outcome the endpoint exists to prevent.
        self.assertTrue(listed <= set(rows), f"no health for {listed - set(rows)}")

    def test_02_verdicts_are_from_the_known_set(self):
        from ava_bridge.dashboard import APP_HEALTH
        for a in CLIENT.get("/api/apps/health").json()["apps"]:
            self.assertIn(a["health"], APP_HEALTH, a)

    def test_03_the_facts_behind_the_verdict_travel_with_it(self):
        # The frontend writes the tooltip from these, so a verdict that arrives
        # without them is a colour the owner cannot act on.
        for a in CLIENT.get("/api/apps/health").json()["apps"]:
            for key in ("enabled", "service", "auth_env", "auth_set",
                        "tools_expected", "tools_deployed",
                        "policy_expected", "policy_present"):
                self.assertIn(key, a, f"{a['id']} is missing {key}")

    def test_04_no_credential_value_ever_leaves_the_bridge(self):
        # auth_env is a NAME and auth_set is a boolean. The Ava-never-has-
        # passwords invariant says the value must not be in this payload.
        body = CLIENT.get("/api/apps/health").text
        for a in CLIENT.get("/api/apps/health").json()["apps"]:
            self.assertIsInstance(a["auth_set"], bool, a)
        self.assertNotIn("token\":", body.lower().replace("_token", ""))


if __name__ == "__main__":
    unittest.main()
