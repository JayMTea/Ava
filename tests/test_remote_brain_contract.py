"""The agent shim NAMES its model, and the bridge reads the name it sends.

THE BUG THIS EXISTS TO PREVENT. `RemoteRuntime.available()` reads `model` and
`provider` off the shim's /healthz into the cache `sandbox_info()` serves, and
`models.effective_brain()` calls `sandbox_info()` to answer "what is Ava
thinking with". The shim's /healthz never sent either key. Neither file was
wrong when read alone — `remote.py` even documents the absence as a tolerated
case — so both passed review, both passed their own tests, and on every
`agent.runtime: remote` install the brain resolved to an EMPTY model id. The
public /api/health advertised `"model": ""`, the chat header rendered its
"Connect a model" first-run chip, and Setup -> Agent reported "No model is
configured, so there is nothing to answer with" — over a sandbox holding a
correctly onboarded 30B and a vLLM answering every request it was given.

The defect was not in either half. It was in the SEAM, and a seam is only
covered by a test that holds both sides at once. So these tests drive the REAL
`agent_runtime_server.app` through a TestClient and point a REAL `RemoteRuntime`
at it, rather than at the hand-written fake shim in test_remote_runtime.py — a
fake that agrees with the adapter proves the adapter agrees with the fake.

House style follows tests/test_remote_runtime.py: stdlib unittest, no container,
no network, `requests.*` translated into the TestClient.

Run: .venv/bin/python -m pytest tests/test_remote_brain_contract.py -q
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-brain-contract-"))

from fastapi.testclient import TestClient

from ava_bridge import agent_runtime_server as shim
from ava_bridge import config, models
from ava_bridge import runtime as runtime_mod
from ava_bridge.runtime import nemoclaw_registry
from ava_bridge.runtime.remote import RemoteRuntime

TOKEN = "shared-agent-token"
SANDBOX = "my-assistant"
# Deliberately an invented vendor and model. What this file tests is that a
# string survives the seam intact, so the string must not be anyone's real one —
# tests/test_no_owner_identity.py bans owner-identifying model ids, and an
# allow-list entry is not the right price for test data that can just be fake.
MODEL = "acme/Reasoner-30B-A3B-Instruct"
PROVIDER = "compatible-endpoint"

#: A registry record trimmed to the fields this seam reads. The live entry
#: carries ~30 more keys; including them here would test nothing and rot.
RECORD = {"name": SANDBOX, "model": MODEL, "provider": PROVIDER,
          "endpointUrl": "http://bridge:8010/v1"}


class _Wiring(unittest.TestCase):
    """A real RemoteRuntime pointed at the real shim app."""

    #: What the registry answers. Overwrite in a test to change the world.
    record: dict | None = RECORD
    #: Whether the shim's NemoClaw runtime reports the sandbox usable.
    ready: bool = True

    def setUp(self):
        self.rt = RemoteRuntime()
        self.rt._avail_cache = {"ts": 0.0, "ok": None, "caps": [], "why": "",
                                "model": "", "provider": ""}
        self.client = TestClient(shim.app, base_url="http://localhost")
        self._patches = [
            mock.patch.object(config, "AGENT_URL", "http://agent:9100"),
            mock.patch.object(config, "AGENT_TOKEN", TOKEN),
            mock.patch.object(config, "AGENT_ENABLED", True),
            mock.patch.object(config, "OC_SANDBOX", SANDBOX),
            # The shim's own runtime. Left real, `healthz` shells out to
            # `nemoclaw list` and the suite pays a 15s timeout per request for
            # coverage of somebody else's CLI.
            mock.patch.object(shim._rt, "available", lambda: self.ready),
            mock.patch.object(nemoclaw_registry, "registry_record",
                              self._registry_record),
            mock.patch("ava_bridge.runtime.remote.requests.get", self._get),
            mock.patch("ava_bridge.runtime.remote.requests.post", self._post),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def _registry_record(self, sandbox=None, max_age=None):
        if isinstance(self.record, Exception):
            raise self.record
        return self.record

    # -- requests -> TestClient ---------------------------------------------
    @staticmethod
    def _wrap(resp):
        resp.ok = resp.is_success      # the requests attribute the adapter reads
        return resp

    def _rel(self, url):
        return url.replace("http://agent:9100", "")

    def _get(self, url, headers=None, timeout=None):
        return self._wrap(self.client.get(self._rel(url), headers=headers))

    def _post(self, url, json=None, headers=None, timeout=None):
        return self._wrap(self.client.post(self._rel(url), json=json,
                                           headers=headers))

    def _healthz(self, token: str | None = TOKEN) -> dict:
        hdr = {"X-Ava-Agent-Token": token} if token else {}
        r = self.client.get("/healthz", headers=hdr)
        self.assertEqual(r.status_code, 200)
        return r.json()


class ShimNamesItsModel(_Wiring):
    """The half that was missing: /healthz carries the sandbox's model."""

    def test_healthz_reports_the_registrys_model_and_provider(self):
        body = self._healthz()
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["provider"], PROVIDER)

    def test_it_advertises_the_capability_it_now_has(self):
        """The flag is the ONLY thing that separates "the sandbox has no model"
        from "this container is too old to say" — both of which arrive as an
        absent key. Ship the field without the flag and the bridge is back to
        one sentence for two faults on two different machines."""
        self.assertIn("health.model", self._healthz()["capabilities"])

    def test_the_capability_list_is_additive(self):
        """Dropping an entry is a breaking change: the bridge fails closed on
        capabilities it cannot see, so a removal silently narrows what a working
        install is allowed to ask for."""
        for cap in ("provision.scope", "provision.assert", "provision.connector"):
            self.assertIn(cap, shim.CAPABILITIES)

    def test_an_unreadable_registry_omits_the_keys_and_does_not_raise(self):
        """"We could not look" must not harden into "there is no model", and a
        health route that raises is worse than one that under-reports."""
        self.record = OSError("permission denied on ~/.nemoclaw")
        body = self._healthz()
        self.assertTrue(body["ok"])
        self.assertNotIn("model", body)
        self.assertNotIn("provider", body)

    def test_a_sandbox_with_no_model_sends_no_model_key(self):
        self.record = {"name": SANDBOX}
        body = self._healthz()
        self.assertNotIn("model", body)

    def test_a_provider_never_travels_without_a_model(self):
        """`effective_brain` renders provider as the ENGINE. Alone it labels a
        blank brain — an engine name beside nothing."""
        self.record = {"name": SANDBOX, "provider": PROVIDER}
        self.assertNotIn("provider", self._healthz())

    def test_naming_the_model_does_not_require_a_token(self):
        """/healthz is the one unauthenticated route by design, and the bridge
        reads the model from it on the same probe that decides availability. If
        the model needed auth, a token mismatch would present as a missing
        model — the exact confusion `authed` was added to end."""
        self.assertEqual(self._healthz(token=None)["model"], MODEL)


class TheBridgeReadsIt(_Wiring):
    """The other half, against the real shim rather than a fake that agrees."""

    def test_sandbox_info_returns_what_the_shim_sent(self):
        info = self.rt.sandbox_info(wait=False)
        self.assertIsNotNone(
            info, "the bridge could not name a model the shim reported")
        self.assertEqual(info["model"], MODEL)
        self.assertEqual(info["provider"], PROVIDER)

    def test_the_name_arrives_on_the_FIRST_probe(self):
        """Not "eventually". `sandbox_info(wait=False)` is reached from the
        public /api/health, and the resolver excuses an empty id as
        self-correcting "when the background refresh lands". There is no
        background refresh on this path — the value rides the health probe — so
        first-call correctness is the whole contract."""
        self.rt._avail_cache = {"ts": 0.0, "ok": None, "caps": [], "why": "",
                                "model": "", "provider": ""}
        self.assertEqual(self.rt.sandbox_info(wait=False)["model"], MODEL)

    def test_effective_brain_names_the_sandbox_model(self):
        """The end of the chain, and the surface the owner actually reads."""
        with mock.patch.object(runtime_mod, "active", lambda: self.rt):
            brain = models.effective_brain()
        self.assertEqual(brain["source"], "agent")
        self.assertEqual(brain["model_id"], MODEL,
                         "Ava still cannot name the model her sandbox runs")
        self.assertEqual(brain["engine"], PROVIDER)
        self.assertEqual(brain["label"], MODEL.rsplit("/", 1)[-1])

    def test_an_empty_registry_is_an_honest_unknown_not_an_invention(self):
        """None means "I do not know". The one thing this seam must never do is
        answer with a model nobody onboarded."""
        self.record = None
        self.assertIsNone(self.rt.sandbox_info(wait=False))

    def test_a_shim_that_cannot_name_a_model_is_still_available(self):
        """Naming the brain and being usable are separate questions. Conflating
        them would take a working agent offline over a cosmetic gap — the
        inverse of the bug this file is about, and just as bad."""
        self.record = None
        self.assertTrue(self.rt.available())

    def test_a_shim_that_is_not_ready_is_not_available(self):
        self.ready = False
        self.assertFalse(self.rt.available())


class TheSeamItself(_Wiring):
    """The guard that would have caught the original defect.

    Both halves above can pass while the seam rots, if a future field is added
    to one side only — which is precisely how `model` came to be read by a
    bridge that no shim ever sent it to. So: enumerate what the adapter reads
    out of the health body, and require the shim to actually emit it.
    """

    #: Keys `available()` reads that the shim is not obliged to send, each with
    #: the reason. A ratchet in the style of tests/test_one_brain_resolver.py:
    #: an entry without a reason is how an allow-list becomes a permanent hole.
    OPTIONAL = {
        "authed": "only present when a token was offered, so an anonymous "
                  "prober learns nothing it could not learn from a 401",
    }

    def _keys_the_shim_can_send(self) -> set:
        """Every key /healthz emits in ANY branch, observed rather than parsed."""
        keys = set(self._healthz(token=TOKEN))       # authed branch
        keys |= set(self._healthz(token=None))       # anonymous branch
        return keys

    def _keys_the_bridge_reads(self) -> set:
        import inspect
        import re
        src = inspect.getsource(RemoteRuntime.available)
        return set(re.findall(r"body\.get\(\s*[\"']([a-z_]+)[\"']", src))

    def test_the_scan_finds_something(self):
        """A vacuous pass is the failure mode every guard in this repo watches
        for — see test_event_vocabulary's own scan-finds-something test."""
        read = self._keys_the_bridge_reads()
        self.assertIn("model", read, "the regex stopped matching available()")
        self.assertGreaterEqual(len(read), 3)

    def test_every_field_the_bridge_reads_is_a_field_the_shim_sends(self):
        missing = self._keys_the_bridge_reads() - self._keys_the_shim_can_send()
        missing -= set(self.OPTIONAL)
        self.assertFalse(
            missing,
            f"RemoteRuntime.available() reads {sorted(missing)} out of the "
            f"/healthz body, and ava_bridge/agent_runtime_server.py never sends "
            f"{'it' if len(missing) == 1 else 'them'}. That is the exact shape "
            f"of the bug this file exists for: the bridge resolves the value to "
            f"empty, models.effective_brain() reports source='agent' with "
            f"nothing in it, and every surface tells the owner no model is "
            f"configured while the sandbox runs one. Send the field from "
            f"healthz(), or stop reading it in available().")

    def test_the_optional_list_has_not_grown_silently(self):
        """Each entry is a hole this guard cannot see through. Adding one should
        be a deliberate act with a reason attached, not a way past a red test."""
        self.assertEqual(set(self.OPTIONAL), {"authed"})


if __name__ == "__main__":
    unittest.main()
