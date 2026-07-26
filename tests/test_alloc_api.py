"""The lease HTTP surface: authenticated, non-blocking, and safe to depend on.

`/lease` exists so a process in another repo (or another venv) can coordinate memory
without importing Ava. Three properties matter:

  * **It is authenticated.** Unlike `/fit`, which only reads, this endpoint can stop
    and start models — an unauthenticated caller could take Ava's brain down. Guarded
    by prefix, because the sub-paths carry a lease id that an exact-match list cannot
    express.
  * **A remote holder is reaped when it stops renewing.** A local holder proves
    liveness with a file lock the kernel releases on death; a caller reaching us over
    HTTP cannot, so the lease carries a deadline. Without reaping, a client killed
    mid-work would hold memory reserved forever.
  * **A client degrades rather than failing.** If the allocator errors, the endpoint
    still answers "proceed" — coordination is advisory, and a render must never fail
    because coordination was unavailable.

Built as a minimal app with just the router mounted (the repo's "minimal apps over the
real app" pattern), so there is no bridge, no GPU and no network.

Run: .venv/bin/python -m pytest tests/test_alloc_api.py -q
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ["AVA_HOME"] = tempfile.mkdtemp(prefix="ava-allocapi-test-")

from fastapi.testclient import TestClient  # noqa: E402

from ava_bridge import router_app  # noqa: E402
from ava_bridge.alloc import broker, ledger  # noqa: E402

TOKEN = "test-router-token"
BACKENDS = [{"id": "b1", "url": "http://127.0.0.1:9/v1", "model": "m",
             "label": "b1", "engine": "vllm", "api_key": None, "tools": "native",
             "fit": None}]


class LeaseApiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ledger-api-")
        p = mock.patch.object(ledger, "_dir", lambda: self.dir)
        p.start()
        self.addCleanup(p.stop)
        app = router_app.create_app(backends=list(BACKENDS), token=TOKEN)
        self.c = TestClient(app)
        self.auth = {"X-Ava-Router-Token": TOKEN}

    # --- auth --------------------------------------------------------------- #
    def test_every_lease_route_requires_the_token(self):
        for method, path in (("post", "/lease"), ("get", "/lease"),
                             ("get", "/lease/abc"),
                             ("post", "/lease/abc/heartbeat"),
                             ("delete", "/lease/abc"),
                             ("post", "/lease/restore")):
            # Only the body-carrying verbs take json= on this client.
            kw = {"json": {}} if method == "post" else {}
            r = getattr(self.c, method)(path, **kw)
            self.assertEqual(r.status_code, 401, f"{method.upper()} {path} was open")

    def test_the_token_grants_access(self):
        r = self.c.get("/lease", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertIn("enforcing", r.json())

    def test_healthz_stays_open(self):
        # A client probes readiness before it has a token in hand.
        self.assertEqual(self.c.get("/healthz").status_code, 200)

    # --- contract ----------------------------------------------------------- #
    def test_a_model_is_required(self):
        r = self.c.post("/lease", json={}, headers=self.auth)
        self.assertEqual(r.status_code, 400)

    def test_acquiring_an_undeclared_model_is_granted_and_ungoverned(self):
        r = self.c.post("/lease", json={"model": "not-declared"}, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["granted"])
        self.assertIn("not declared", body["reason"])

    def test_an_allocator_error_still_answers_proceed(self):
        # Coordination is advisory: a failure here must not fail the caller's work.
        with mock.patch.object(broker, "acquire_api", side_effect=RuntimeError("boom")):
            r = self.c.post("/lease", json={"model": "x"}, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["granted"])
        self.assertIn("proceed", r.json()["reason"])

    def test_heartbeat_on_an_unknown_lease_is_reported_not_fatal(self):
        r = self.c.post("/lease/nope/heartbeat", json={}, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])

    def test_release_is_idempotent(self):
        for _ in range(2):
            r = self.c.delete("/lease/nope", headers=self.auth)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])

    def test_polling_an_unknown_lease_is_gone_not_pending(self):
        # A poller must be able to stop. "Gone" is terminal; anything that reads as
        # pending would be polled forever for work nobody is doing.
        r = self.c.get("/lease/nope", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "gone")
        self.assertFalse(r.json()["granted"])

    def test_wait_false_reaches_the_broker(self):
        # The default must stay `wait=True`, so a client written against the older
        # contract is never told it may start before the memory is free.
        with mock.patch.object(broker, "acquire_api",
                               return_value={"ok": True}) as spy:
            self.c.post("/lease", json={"model": "m"}, headers=self.auth)
            self.assertIs(spy.call_args.kwargs["wait"], True)
            self.c.post("/lease", json={"model": "m", "wait": False},
                        headers=self.auth)
            self.assertIs(spy.call_args.kwargs["wait"], False)

    def test_an_ungoverned_model_is_ready_immediately(self):
        # Nothing to wait for means the client must not be sent into a poll loop.
        body = self.c.post("/lease", json={"model": "not-declared", "wait": False},
                           headers=self.auth).json()
        self.assertEqual(body["state"], "active")
        self.assertTrue(body["ready"])


class RemoteReapTests(unittest.TestCase):
    """A remote holder that stops renewing must not keep memory reserved."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ledger-reap-")
        self.t = 5000.0
        for p in (mock.patch.object(ledger, "_dir", lambda: self.dir),
                  mock.patch.object(ledger, "now", lambda: self.t)):
            p.start()
            self.addCleanup(p.stop)

    def _remote_lease(self, ttl=60.0):
        lid = ledger.new_lease_id()
        ledger.write_lease(lid, {"models": ["m"], "remote": True,
                                 "holder": "other-app", "ttl_s": ttl,
                                 "expires_at": self.t + ttl})
        # A remote lease has no local lock, so make it appear live the way the ledger
        # decides liveness — by holding one for the duration of the test.
        self.keeper = broker._LockKeeper(ledger.lease_lock_name(lid))
        self.keeper.take()
        self.addCleanup(self.keeper.release)
        return lid

    def test_a_live_remote_lease_is_not_reaped(self):
        lid = self._remote_lease(ttl=60.0)
        self.assertEqual(broker.reap_expired(), [])
        self.assertIsNotNone(ledger.read_lease(lid))

    def test_a_lapsed_remote_lease_is_reaped(self):
        lid = self._remote_lease(ttl=60.0)
        self.t += 61.0                      # the holder stopped renewing
        self.assertEqual(broker.reap_expired(), [lid])

    def test_heartbeat_extends_the_deadline(self):
        lid = self._remote_lease(ttl=60.0)
        self.t += 50.0
        broker.heartbeat_api(lid, ttl_s=60.0)
        self.t += 30.0                      # would have lapsed without the renewal
        self.assertEqual(broker.reap_expired(), [])

    def test_a_local_lease_is_never_reaped_on_a_deadline(self):
        # Local liveness is the kernel releasing a lock, not a clock. Applying a
        # deadline to it would evict a caller that is still working.
        lid = ledger.new_lease_id()
        ledger.write_lease(lid, {"models": ["m"]})       # no remote flag
        keeper = broker._LockKeeper(ledger.lease_lock_name(lid))
        keeper.take()
        self.addCleanup(keeper.release)
        self.t += 100_000.0
        self.assertEqual(broker.reap_expired(), [])


if __name__ == "__main__":
    unittest.main()
