"""Verification must refuse to claim more than it actually checked.

`provision.verify()` is what turns "install.sh exited 0" into "the sandbox really
holds what this checkout declares". Its dangerous failure is not missing a
problem — it is *inventing certainty*: reporting `verified: False` for something
it could not look at, or `verified: True` from install.sh's own record of what it
attempted.

Three rules are pinned here:

  * A probe that could not run yields `verified: None`. Never False, never True.
  * install.sh's skills manifest can prove ABSENCE but never SUCCESS — it writes a
    row for a skill whose install failed, by design, because one bad skill must
    not strand the deploy.
  * Registry-sourced checks still run with the container stopped, because that is
    the state in which the owner most wants an answer.

Nothing here touches a real runtime or the real `nemoclaw` binary.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from unittest import mock

from ava_bridge import provision


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


WANT = {
    "persona": [{"id": "IDENTITY.md", "label": "Persona", "sha256": _sha("p"),
                 "rel": "agent/persona.txt.tmpl"}],
    "policies": [{"id": "ava-weather", "label": "ava-weather", "sha256": _sha("w"),
                  "rel": "agent/policies/ava-weather.yaml"}],
    # `sha256` is a fold of the whole server TREE, and `sandbox_root` is what it
    # gets folded against. The fixture used to omit the root and let the fake
    # answer with an entry-point digest that happened to equal this value —
    # which is exactly the mismatch production had, papered over.
    "servers": [{"id": "ava-admin", "label": "ava-admin", "sha256": _sha("s"),
                 "rel": "agent/mcp_server_admin",
                 "path": "/sandbox/.openclaw/mcp_server_admin/_server.mjs",
                 "sandbox_root": "/sandbox/.openclaw/mcp_server_admin"}],
    "skills": [{"id": "weather", "label": "Weather", "sha256": _sha("k"),
                "rel": "agent/skills/weather/SKILL.md", "sandbox_id": "weather"}],
}


class _Rt:
    sandbox = "test-sandbox"

    def __init__(self, *, record=None, live=True, digests=None, openclaw=None,
                 globs=None, trees=None):
        self._record, self._live = record, live
        self._digests, self._openclaw, self._globs = digests or {}, openclaw, globs
        # `None` means this runtime cannot fold a tree at all (the RemoteRuntime
        # case), which must read `unknown` rather than an empty sandbox.
        self._trees = trees
        self.digest_calls = 0

    def registry_record(self):
        return self._record

    def live(self):
        return {"live": self._live,
                "reason": "" if self._live else "the sandbox container is not running"}

    def digest(self, paths, timeout=30):
        self.digest_calls += 1
        return {p: self._digests[p] for p in paths if p in self._digests}

    def glob_digest(self, pattern, timeout=30):
        return dict(self._globs or {})

    def tree_digests(self, roots, timeout=30):
        if self._trees is None:
            return None
        return {r: self._trees[r] for r in roots if r in self._trees}

    def read_file(self, path, timeout=20):
        return self._openclaw


def _verify(rt, scope="all", manifest=None, want=None, connector=None):
    with mock.patch.object(provision, "desired", return_value=want or WANT), \
         mock.patch.object(provision, "_manifest_map", return_value=manifest), \
         mock.patch.object(provision, "load_run", return_value=None):
        provision.invalidate()
        return provision.verify(rt=rt, scope=scope, connector=connector)


class LivenessGateTests(unittest.TestCase):

    def test_a_stopped_sandbox_yields_none_not_false(self):
        """`verified: False` means "we looked and it is wrong". With the container
        down we did not look, and saying otherwise is the exact lie this guards."""
        rt = _Rt(record=None, live=False)
        res = _verify(rt)
        self.assertFalse(res["live"])
        for item in res["items"]:
            self.assertIsNone(item["verified"], f"{item['scope']}/{item['id']}")
        self.assertEqual(res["failed"], [])

    def test_a_stopped_sandbox_does_not_run_probes_at_all(self):
        rt = _Rt(record=None, live=False)
        _verify(rt)
        self.assertEqual(rt.digest_calls, 0,
                         "a probe was attempted against a stopped sandbox")

    def test_registry_checks_still_run_with_the_container_stopped(self):
        """The payoff of reading the NemoClaw registry instead of only probing."""
        rt = _Rt(record={"customPolicies": []}, live=False)
        res = _verify(rt, scope="policies")
        self.assertTrue(res["checked"])
        self.assertEqual(res["failed"], ["policies/ava-weather"])
        pol = next(i for i in res["items"] if i["scope"] == "policies")
        self.assertFalse(pol["verified"])
        self.assertIn("not present", pol["verify_detail"])


class ProbeTests(unittest.TestCase):

    def _live_rt(self, *, persona=_sha("p"), server=_sha("s"), skill=_sha("k")):
        return _Rt(
            record={"customPolicies": [{"name": "ava-weather", "content": "w"}]},
            live=True,
            digests={provision.PERSONA_PATH: persona,
                     "/sandbox/.openclaw/mcp_server_admin/_server.mjs": server},
            openclaw=('{"mcp":{"servers":{"ava-admin":{"command":"node",'
                      '"args":["/sandbox/.openclaw/mcp_server_admin/_server.mjs"]}}}}'),
            globs={"/sandbox/.openclaw/skills/weather/SKILL.md": skill},
            trees={"/sandbox/.openclaw/mcp_server_admin": server},
        )

    def test_everything_matching_verifies_clean(self):
        res = _verify(self._live_rt())
        self.assertTrue(res["checked"])
        self.assertEqual(res["failed"], [])
        self.assertTrue(all(i["verified"] for i in res["items"]))

    def test_a_persona_that_differs_reads_as_content_drift(self):
        res = _verify(self._live_rt(persona=_sha("OLD")))
        self.assertIn("persona/IDENTITY.md", res["failed"])
        item = next(i for i in res["items"] if i["scope"] == "persona")
        self.assertFalse(item["verified"])
        self.assertIn("content differs", item["verify_detail"])

    def test_an_unregistered_server_is_reported_absent(self):
        rt = self._live_rt()
        rt._openclaw = '{"mcp":{"servers":{}}}'
        res = _verify(rt)
        self.assertIn("servers/ava-admin", res["failed"])

    def test_a_server_registered_under_the_wrong_path_is_not_credited(self):
        rt = self._live_rt()
        rt._openclaw = ('{"mcp":{"servers":{"ava-admin":{"command":"node",'
                        '"args":["/somewhere/else/_server.mjs"]}}}}')
        res = _verify(rt)
        self.assertIn("servers/ava-admin", res["failed"])

    def test_a_live_skill_digest_beats_the_manifest(self):
        """The manifest says the skill was installed; the sandbox says it differs.
        The sandbox wins — the manifest records intent, not outcome."""
        rt = self._live_rt(skill=_sha("EDITED"))
        res = _verify(rt, manifest={"weather": _sha("k")})
        self.assertIn("skills/weather", res["failed"])


class ManifestHonestyTests(unittest.TestCase):

    def test_the_manifest_alone_never_yields_a_positive_verdict(self):
        """install.sh writes a manifest row even when `skill install` FAILED —
        deliberately, so one bad skill cannot strand the deploy. So a manifest
        match is not evidence the skill reached the sandbox."""
        rt = _Rt(record={"customPolicies": [{"name": "ava-weather", "content": "w"}]},
                 live=True, digests={}, openclaw=None, globs={})
        res = _verify(rt, scope="skills", manifest={"weather": _sha("k")})
        item = next(i for i in res["items"] if i["scope"] == "skills")
        self.assertIsNone(item["verified"],
                          "a manifest match was reported as a verified deployment")
        self.assertFalse(res["checked"])


class VerifyStepTests(unittest.TestCase):

    def test_an_unchecked_result_produces_no_step(self):
        self.assertIsNone(provision.verify_step(
            {"checked": False, "items": [], "failed": []}))

    def test_a_clean_result_produces_a_green_step(self):
        step = provision.verify_step({"checked": True, "items": [{}, {}], "failed": []})
        self.assertTrue(step["ok"])
        self.assertEqual(step["step"], "verify")

    def test_failures_are_named_in_the_step_detail(self):
        step = provision.verify_step(
            {"checked": True, "items": [{}],
             "failed": ["policies/ava-weather", "skills/notes"]})
        self.assertFalse(step["ok"])
        self.assertIn("ava-weather", step["detail"])
        self.assertIn("2 item(s)", step["detail"])

    def test_a_long_failure_list_is_summarised_not_dumped(self):
        step = provision.verify_step(
            {"checked": True, "items": [{}],
             "failed": [f"skills/s{i}" for i in range(9)]})
        self.assertIn("+6 more", step["detail"])
        self.assertLess(len(step["detail"]), 160)


class ScopeTests(unittest.TestCase):

    def test_verifying_one_scope_ignores_the_others(self):
        rt = _Rt(record={"customPolicies": []}, live=False)
        res = _verify(rt, scope="policies")
        self.assertEqual({i["scope"] for i in res["items"]}, {"policies"})

    def test_a_comma_scope_is_the_union_of_both(self):
        rt = _Rt(record={"customPolicies": []}, live=False)
        res = _verify(rt, scope="policies,servers")
        self.assertEqual({i["scope"] for i in res["items"]}, {"policies", "servers"})

    def test_an_unknown_token_verifies_nothing_rather_than_everything(self):
        """`wanted` collapsing to empty is how the post-apply veto silently
        switches itself off: no rows, no `checked`, no step, and the run reports
        green with nothing asserted. The gates reject the token before it can get
        here — this pins the behaviour if one ever leaks through."""
        rt = _Rt(record={"customPolicies": []}, live=False)
        res = _verify(rt, scope="connector:acme")
        self.assertEqual(res["items"], [])
        self.assertFalse(res["checked"])
        self.assertIsNone(provision.verify_step(res),
                          "an unverifiable result produced a step, which would "
                          "read as a verdict")


class ConnectorScopedVerifyTests(unittest.TestCase):
    """A per-connector deploy must be judged on what IT did.

    `deploy_connector` used to run a FULL provision, so it also applied every
    other connector's policy on the way past — and verify(scope="all") was
    therefore fair. Narrowing the deploy without narrowing the assert means
    connector B's undeployed policy vetoes connector A's successful run: a green
    deploy reported as a failure, which is the shape the entry-point-vs-tree bug
    had, arriving from the other direction.
    """

    #: Two connectors. `acme` is applied in the sandbox; `ghost` never was.
    WANT_TWO = {
        "persona": [],
        "policies": [
            {"id": "ava-acme", "label": "ava-acme", "sha256": _sha("a"),
             "rel": "agent/policies/generated/acme.yaml"},
            {"id": "ava-ghost", "label": "ava-ghost", "sha256": _sha("g"),
             "rel": "agent/policies/generated/ghost.yaml"},
        ],
        "servers": [
            {"id": "ava-tools-connectors", "label": "ava-tools-connectors",
             "sha256": _sha("c"), "rel": "agent/mcp_server_connectors",
             "path": "/sandbox/.openclaw/mcp_server_connectors/_server.mjs",
             "sandbox_root": "/sandbox/.openclaw/mcp_server_connectors"},
        ],
        "skills": [],
    }

    def _rt(self):
        # `acme`'s policy is applied; `ghost`'s is not. The connectors server
        # matches this checkout.
        return _Rt(record={"customPolicies": [{"name": "ava-acme", "content": "a"}]},
                   live=True,
                   openclaw=json.dumps({"mcp": {"servers": {"ava-tools-connectors": {
                       "command": "node",
                       "args": ["/sandbox/.openclaw/mcp_server_connectors/_server.mjs"]}}}}),
                   trees={"/sandbox/.openclaw/mcp_server_connectors": _sha("c")},
                   digests={"/sandbox/.openclaw/mcp_server_connectors/_server.mjs": "x"})

    def test_another_connectors_drift_does_not_veto_this_run(self):
        res = _verify(self._rt(), scope="policies,servers",
                      want=self.WANT_TWO, connector="acme")
        self.assertEqual(res["failed"], [],
                         "deploying 'acme' was vetoed by 'ghost', which this run "
                         "never claimed to deploy")
        step = provision.verify_step(res)
        self.assertIsNotNone(step, "nothing was asserted at all")
        self.assertTrue(step["ok"],
                        f"the post-apply step failed a run that did its job: {step}")

    def test_it_still_checks_the_connectors_own_rows(self):
        res = _verify(self._rt(), scope="policies,servers",
                      want=self.WANT_TWO, connector="acme")
        self.assertEqual({i["id"] for i in res["items"]},
                         {"ava-acme", "ava-tools-connectors"},
                         "the narrowed verify checked the wrong rows")
        self.assertTrue(res["checked"], "it asserted nothing at all")

    def test_this_connectors_own_failure_still_vetoes(self):
        """Narrowing must not become a way to never fail. `ghost` has no policy
        in the sandbox, so deploying GHOST must report it."""
        res = _verify(self._rt(), scope="policies,servers",
                      want=self.WANT_TWO, connector="ghost")
        self.assertIn("policies/ava-ghost", res["failed"])
        step = provision.verify_step(res)
        self.assertIsNotNone(step)
        self.assertFalse(step["ok"])

    def test_an_ava_prefixed_connector_id_is_not_double_prefixed(self):
        """`render_egress_policy` deliberately does NOT double the prefix — a
        connector called `ava-notes` keeps the preset `ava-notes`, pinned by
        tests/test_connectors.py. Re-deriving that rule here got it wrong for
        exactly the ids the special case exists for, and the failure was silent:
        the row never matched, so the deploy asserted nothing about its own
        policy and reported success either way."""
        from ava_bridge import connectors

        self.assertEqual(connectors.policy_preset_name("ava-notes"), "ava-notes")
        self.assertEqual(connectors.policy_preset_name("notes"), "ava-notes")
        self.assertEqual(provision.connector_items("ava-notes")["policies"],
                         {"ava-notes"})

    def test_the_filter_uses_the_same_rule_the_renderer_does(self):
        """One rule, two readers — asserted directly so a second copy cannot
        drift back in."""
        from ava_bridge import connectors

        for cid in ("acme", "ava-notes", "ava", "a-b_c"):
            self.assertEqual(provision.connector_items(cid)["policies"],
                             {connectors.policy_preset_name(cid)}, cid)

    def test_an_unnarrowed_run_still_sees_everything(self):
        res = _verify(self._rt(), scope="policies,servers", want=self.WANT_TWO)
        self.assertIn("policies/ava-ghost", res["failed"])


if __name__ == "__main__":
    unittest.main()
