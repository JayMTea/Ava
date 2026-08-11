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


def _verify(rt, scope="all", manifest=None):
    with mock.patch.object(provision, "desired", return_value=WANT), \
         mock.patch.object(provision, "_manifest_map", return_value=manifest), \
         mock.patch.object(provision, "load_run", return_value=None):
        provision.invalidate()
        return provision.verify(rt=rt, scope=scope)


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


if __name__ == "__main__":
    unittest.main()
