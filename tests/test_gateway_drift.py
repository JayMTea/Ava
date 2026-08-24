"""Drift, as answered by a runtime with a control plane instead of a shell.

The four-state ladder's entire value is that `unknown` and `undeployed` are
different claims: "we could not look" versus "we looked and it is gone". A
gateway runtime has no `exec`, so it can answer some scopes and not others — and
the dangerous failure is not being unable to answer, it is answering ANYWAY with
an empty map, which renders as a to-do list of things that are in fact live.

So every test here is about restraint: what the gateway declines to claim, and
what survives when it declines.

House style: stdlib unittest, no bridge, no sandbox, no network.
"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-gwdrift-test-"))

import importlib

from ava_bridge import provision
from ava_bridge.runtime.errors import GatewayError

# `ava_bridge.runtime` exposes accessor FUNCTIONS named after its modules
# (`runtime.nemoclaw()`, `runtime.openclaw_gw()`), so `from ava_bridge.runtime
# import openclaw_gw` binds the function and not the module. Pre-existing and
# consistent across all four adapters, so it is worked around here rather than
# renamed.
openclaw_gw = importlib.import_module("ava_bridge.runtime.openclaw_gw")


class FakeClient:
    def __init__(self, methods=(), answers=None, raise_on=()):
        self._methods = frozenset(methods)
        self._answers = answers or {}
        self._raise_on = set(raise_on)
        self.calls: list[str] = []

    def methods(self):
        return self._methods

    def rpc(self, method, params=None, *, timeout=None, idempotency_key=None):
        self.calls.append(method)
        if method in self._raise_on:
            raise GatewayError("nope", "gateway_rpc_failed")
        return self._answers.get(method, {})

    def status(self):
        return {"phase": "ready"}

    def start(self):
        pass


def _rt(client) -> openclaw_gw.OpenClawGatewayRuntime:
    return openclaw_gw.OpenClawGatewayRuntime(client=client)


WANT = {"persona": [{"id": "IDENTITY.md", "sha256": "x"}],
        "policies": [{"id": "ava-weather", "sha256": "y"}],
        "servers": [{"id": "ava-admin", "sha256": "z", "path": "/p"}],
        "skills": [{"id": "ava-web", "sandbox_id": "web", "sha256": "k"}]}


class RestraintTests(unittest.TestCase):

    def test_a_gateway_that_offers_nothing_claims_nothing(self):
        got = _rt(FakeClient()).observe(WANT)
        self.assertEqual(got["maps"], {},
                         "a runtime that cannot look must not report an empty "
                         "sandbox — that is a to-do list of live things")

    def test_policies_are_never_claimed(self):
        """Egress rules are OpenShell/NemoClaw state recorded host-side. The
        OpenClaw gateway does not own them, and `observed()` already reads them
        from the registry — which works with the container stopped."""
        client = FakeClient(methods=["agents.files.get", "plugins.list"],
                            answers={"agents.files.get": {"content": "hi"}})
        got = _rt(client).observe(WANT)
        self.assertNotIn("policies", got["maps"])
        self.assertNotIn("policies", got["sources"])

    def test_servers_are_never_claimed_without_a_file_tree(self):
        """The ladder compares a TREE FOLD both sides. Knowing a server is
        registered cannot tell `deployed` from `stale`, and guessing `deployed`
        is the lie `item_state` exists to prevent. `unknown` is honest."""
        client = FakeClient(methods=["agents.files.get", "plugins.uiDescriptors"],
                            answers={"plugins.uiDescriptors":
                                     {"descriptors": [{"id": "ava-admin"}]}})
        got = _rt(client).observe(WANT)
        self.assertNotIn("servers", got["maps"])

    def test_a_failed_read_omits_the_scope_rather_than_emptying_it(self):
        client = FakeClient(methods=["agents.files.get"],
                            raise_on=["agents.files.get"])
        got = _rt(client).observe(WANT)
        self.assertNotIn("persona", got["maps"])


class PersonaTests(unittest.TestCase):

    def test_the_persona_digest_is_the_file_contents(self):
        client = FakeClient(methods=["agents.files.get"],
                            answers={"agents.files.get": {"content": "PERSONA"}})
        got = _rt(client).observe(WANT)
        self.assertEqual(got["maps"]["persona"],
                         {"IDENTITY.md": openclaw_gw._sha("PERSONA")})
        self.assertEqual(got["sources"]["persona"], "gateway")

    def test_the_persona_path_agrees_with_the_provisioner(self):
        """Two modules naming one file. They are separate on purpose — the
        dependency runs provisioner -> runtime, not back — so the agreement has
        to be asserted rather than assumed."""
        self.assertEqual(openclaw_gw.PERSONA_PATH, provision.PERSONA_PATH)


class SkillTests(unittest.TestCase):

    def test_skills_are_not_observable_through_the_gateway(self):
        """And the scope must say so rather than claim an empty deployment.

        Skills live in `managedSkillsDir` (/sandbox/.openclaw/skills), outside
        the agent workspace that `agents.files.get` serves — it answers
        `unsupported file` for anything above it. `skills.status` lists what is
        installed but returns no CONTENT, and a digest map needs bytes. Nothing
        in the 218-method surface returns a skill's contents.

        So the scope is omitted, `provision.observed` leaves source="none", and
        `item_state` reads that as `unknown` — never `pending`. Two earlier
        tests asserted a digest map here; they passed against a FakeClient that
        offered `agents.files.read`, which no gateway has ever had.
        """
        client = FakeClient(methods=["agents.files.get"],
                            answers={"agents.files.get": {"content": "S"}})
        got = _rt(client).observe(WANT)
        self.assertNotIn("skills", got["maps"],
                         "an unobservable scope must be absent, not empty — "
                         "empty reads as `undeployed` and offers an Apply that "
                         "would not fix it")
        self.assertNotEqual(got["sources"].get("skills"), "gateway")


class ExtrasTests(unittest.TestCase):

    def test_extras_are_none_when_the_method_is_not_offered(self):
        """`None` means "could not look". `[]` would mean "nothing extra", and
        the difference is whether the owner is told the gateway is running
        plugins Ava never declared."""
        got = _rt(FakeClient(methods=["agents.files.get"])).observe(WANT)
        self.assertIsNone(got["extras"]["plugins"])
        self.assertIsNone(got["extras"]["cron"])

    def test_extras_list_what_the_gateway_holds(self):
        # Cron extras read the SCHEDULER, cron.list -> {jobs}, not the in-flight
        # tasks.list which always looks empty. Verified live 2026-08-24.
        client = FakeClient(
            methods=["plugins.uiDescriptors", "cron.list"],
            answers={"plugins.uiDescriptors":
                     {"descriptors": [{"id": "github"}, {"id": "gh"}]},
                     "cron.list": {"jobs": [{"name": "nightly"}]}})
        got = _rt(client).observe(WANT)
        self.assertEqual(got["extras"]["plugins"], ["gh", "github"])
        self.assertEqual(got["extras"]["cron"], ["nightly"])


class MergeTests(unittest.TestCase):
    """`observe()` never sees provision's internal shape, so the merge is what
    keeps it from destroying what was already established."""

    def _out(self):
        return {"record": {"x": 1},
                "maps": {"persona": None, "policies": {"ava-weather": "sha"},
                         "servers": None, "skills": {"ava-web": "m"}},
                "sources": {"persona": "none", "policies": "registry",
                            "servers": "none", "skills": "manifest"}}

    def test_a_scope_the_gateway_answered_wins(self):
        out = self._out()
        provision._merge_observed(
            out, {"maps": {"persona": {"IDENTITY.md": "abc"}},
                  "sources": {"persona": "gateway"}})
        self.assertEqual(out["maps"]["persona"], {"IDENTITY.md": "abc"})
        self.assertEqual(out["sources"]["persona"], "gateway")

    def test_registry_policies_survive_a_gateway_answer(self):
        """The bug this merge exists to prevent: a wholesale update would drop
        the policies read from the registry, which is the ONE source that works
        with the container stopped."""
        out = self._out()
        provision._merge_observed(
            out, {"maps": {"persona": {"IDENTITY.md": "abc"}}, "sources": {}})
        self.assertEqual(out["maps"]["policies"], {"ava-weather": "sha"})
        self.assertEqual(out["sources"]["policies"], "registry")

    def test_manifest_skills_survive_when_the_gateway_stays_silent(self):
        out = self._out()
        provision._merge_observed(out, {"maps": {}, "sources": {}})
        self.assertEqual(out["sources"]["skills"], "manifest")

    def test_an_unknown_scope_name_is_ignored(self):
        out = self._out()
        provision._merge_observed(
            out, {"maps": {"plugins": {"a": "b"}}, "sources": {}})
        self.assertNotIn("plugins", out["maps"],
                         "a fifth scope would break the pending arithmetic")

    def test_extras_ride_alongside_rather_than_inside_the_scopes(self):
        out = self._out()
        provision._merge_observed(
            out, {"maps": {}, "sources": {},
                  "extras": {"plugins": ["gh"], "cron": None}})
        self.assertEqual(out["extras"], {"plugins": ["gh"], "cron": None})


class LadderTests(unittest.TestCase):

    def test_the_new_source_is_treated_as_authoritative(self):
        """`gateway` has the same authority as `probe`: it is the component that
        HOLDS the thing answering, rather than a shell looking at it."""
        self.assertEqual(
            provision.item_state("a", "a", source="gateway"), "deployed")
        self.assertEqual(
            provision.item_state("a", "b", source="gateway"), "stale")
        self.assertEqual(
            provision.item_state("a", None, source="gateway"), "undeployed")

    def test_only_none_still_means_we_could_not_look(self):
        self.assertEqual(provision.item_state("a", None, source="none"), "unknown")

    def test_the_ladder_docstring_documents_every_source_it_can_receive(self):
        """That enumeration IS the contract's documentation; an undocumented
        source means the next reader has to find out by experiment."""
        doc = provision.item_state.__doc__ or ""
        for source in ("registry", "probe", "gateway", "manifest", "none"):
            self.assertIn(source, doc)


class OrphanTests(unittest.TestCase):

    def test_extras_appear_beside_the_scope_orphans(self):
        obs = {"maps": {"policies": {"ava-old": "s"}, "servers": None,
                        "skills": None, "persona": None},
               "sources": {"policies": "registry", "servers": "none",
                           "skills": "none", "persona": "none"},
               "extras": {"plugins": ["github"], "cron": None}}
        want = {s: [] for s in provision.SCOPES}
        got = provision._orphans(obs, want)
        self.assertEqual(got["policies"], ["ava-old"])
        self.assertEqual(got["plugins"], ["github"])
        self.assertIsNone(got["cron"])

    def test_orphan_keys_are_no_longer_a_subset_of_scopes(self):
        """Stated as a test because the old comprehension made it an invariant,
        and anything downstream that still assumes it will now be wrong."""
        obs = {"maps": {s: None for s in provision.SCOPES},
               "sources": {s: "none" for s in provision.SCOPES},
               "extras": {"plugins": []}}
        got = provision._orphans(obs, {s: [] for s in provision.SCOPES})
        self.assertTrue(set(got) - set(provision.SCOPES))


if __name__ == "__main__":
    unittest.main()
