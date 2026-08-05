"""Freeing model memory from the UI: the routes behind the hardware bubble's buttons.

This is the whole-app tier, so it asserts the things only the real app can answer:
that the routes are behind the owner's cookie, that they are POST-only, that a
cross-site POST is refused, and that the report carries every fact the panel needs to
decide what to offer — including on a box like this one, where nothing has a release
lever and the honest answer is "no button".
"""
import unittest

import pytest

from qa import helpers


@pytest.fixture(scope="module", autouse=True)
def _client(bridge):
    helpers.ensure_authed(bridge)
    globals()["CLIENT"] = bridge
    yield


class TestReport(unittest.TestCase):
    def test_the_report_says_whether_allocation_is_even_on(self):
        body = CLIENT.get("/api/hub/hardware/alloc").json()
        self.assertIn("enabled", body)
        # Not a 404 when off: the panel has to be able to say WHY it is empty, and
        # an absent route is indistinguishable from a broken one.
        self.assertIn("models", body)
        self.assertIn("declared_count", body)

    def test_every_row_carries_what_the_panel_needs_to_decide(self):
        body = CLIENT.get("/api/hub/hardware/alloc").json()
        if not body.get("enabled"):
            self.skipTest("allocation is disabled in this environment")
        for row in body["models"]:
            for key in ("id", "driver", "modes", "restore_kind", "observe_only",
                        "released_by_owner", "held_by", "is_brain", "resident",
                        "measured"):
                self.assertIn(key, row, f"{row.get('id')} is missing {key}")
            # `modes` is what decides the button's LABEL — unload and stop are very
            # different promises — so it must be a list, never absent.
            self.assertIsInstance(row["modes"], list)
            # restore_kind is a closed vocabulary; a fourth value would leave the
            # panel guessing which sentence to show.
            self.assertIn(row["restore_kind"], ("none", "self", "start"))

    def test_the_report_carries_the_breaker_so_a_dead_button_can_explain_itself(self):
        body = CLIENT.get("/api/hub/hardware/alloc").json()
        if not body.get("enabled"):
            self.skipTest("allocation is disabled in this environment")
        self.assertIn("breaker", body)

    def test_the_job_slot_starts_idle(self):
        self.assertEqual(CLIENT.get("/api/hub/hardware/alloc/job").json()["status"],
                         "idle")


class TestActuationGuards(unittest.TestCase):
    def test_release_is_post_only(self):
        """A lax session cookie DOES ride a top-level GET navigation.

        So a GET that frees memory would be a one-click drive-by that takes Ava's
        brain down from any page the owner happens to open.
        """
        r = CLIENT.get("/api/hub/hardware/alloc/anything/release")
        self.assertEqual(r.status_code, 405)

    def test_a_cross_site_post_is_refused(self):
        r = CLIENT.post("/api/hub/hardware/alloc/anything/release",
                        headers={"sec-fetch-site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "cross_site")

    def test_an_anonymous_caller_gets_nothing(self):
        anon = helpers.anon_client()
        for path, method in (("/api/hub/hardware/alloc", "get"),
                             ("/api/hub/hardware/alloc/x/release", "post"),
                             ("/api/hub/hardware/alloc/x/restore", "post"),
                             ("/api/hub/hardware/alloc/x/reset", "post")):
            r = getattr(anon, method)(path)
            self.assertEqual(r.status_code, 401, f"{method.upper()} {path} was open")

    def test_an_undeclared_model_is_refused_by_name(self):
        r = CLIENT.post("/api/hub/hardware/alloc/no-such-model/release")
        if r.status_code == 409 and r.json().get("code") == "alloc_disabled":
            self.skipTest("allocation is disabled in this environment")
        # The job runs on a worker, so the refusal arrives in the job result rather
        # than the response — which is the contract the panel polls.
        self.assertEqual(r.status_code, 200, r.text)
        res = _settled()
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "not_declared")


def _settled(timeout: float = 10.0) -> dict:
    """Poll the job slot until it leaves 'running'. Returns its result."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = CLIENT.get("/api/hub/hardware/alloc/job").json()
        if job["status"] != "running":
            return job.get("result") or {}
        time.sleep(0.05)
    raise AssertionError("alloc job still running")
