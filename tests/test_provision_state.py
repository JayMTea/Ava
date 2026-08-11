"""Drift must distinguish "we looked and it isn't there" from "we couldn't look".

`ava_bridge/provision.py` reports what the NemoClaw sandbox is actually running
against what this checkout declares. Every interesting failure of such a report is
a *confident wrong answer*, so these tests are mostly about the states it must
refuse to claim:

  * A stopped sandbox must yield `unknown`, never `undeployed`. Getting this
    backwards turns "I can't see inside the container" into "12 changes to apply",
    the user clicks Apply, and it fails at the bootstrap guard.
  * `unknown` must never count as pending, for the same reason.
  * A rebuild is the one case where absence IS provable with the container down —
    the image was replaced, so everything inside it is gone.
  * On a box with a moved `server.port`, install.sh applies a REWRITTEN policy, so
    comparing the tracked bytes would report every policy stale forever.

House style: stdlib unittest, self-contained, no bridge, no sandbox, no network.
"""
from __future__ import annotations

import hashlib
import json
import os
import unittest
from unittest import mock

from ava_bridge import config, provision


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LadderTests(unittest.TestCase):
    """`item_state` is the whole verdict engine; everything else is plumbing."""

    def test_nothing_could_observe_it_is_unknown_not_undeployed(self):
        self.assertEqual(
            provision.item_state("abc", None, source="none"), "unknown",
            "a scope with no evidence source must be unknown. Reporting it as "
            "undeployed tells the owner to apply changes into a sandbox we never "
            "actually looked at.")

    def test_looked_and_absent_is_undeployed(self):
        self.assertEqual(provision.item_state("abc", None, source="registry"),
                         "undeployed")

    def test_present_but_different_is_stale(self):
        self.assertEqual(provision.item_state("abc", "def", source="registry"),
                         "stale")

    def test_present_and_identical_is_deployed(self):
        self.assertEqual(provision.item_state("abc", "abc", source="registry"),
                         "deployed")

    def test_a_rebuild_makes_absence_provable_even_with_no_probe(self):
        """imageTag moved ⇒ the image was replaced ⇒ its contents are gone.

        The one case where we may assert absence without looking inside, and it is
        cheaper and stronger than any version comparison.
        """
        self.assertEqual(
            provision.item_state("abc", None, source="none", rebuilt=True),
            "undeployed")

    def test_every_state_is_in_the_published_vocabulary(self):
        for source in ("none", "registry", "probe", "manifest"):
            for have in (None, "abc", "zzz"):
                self.assertIn(provision.item_state("abc", have, source=source),
                              provision.STATES)


class RollupTests(unittest.TestCase):
    def test_stale_outranks_undeployed(self):
        """A missing item fails loudly; a stale one lies quietly, so it wins."""
        self.assertEqual(provision._roll_up(["deployed", "undeployed", "stale"]),
                         "stale")

    def test_unknown_outranks_deployed_but_not_a_real_problem(self):
        self.assertEqual(provision._roll_up(["deployed", "unknown"]), "unknown")
        self.assertEqual(provision._roll_up(["unknown", "undeployed"]), "undeployed")

    def test_all_clear_rolls_up_to_deployed(self):
        self.assertEqual(provision._roll_up(["deployed", "deployed"]), "deployed")


class PortRewriteTests(unittest.TestCase):
    def test_the_default_port_is_a_passthrough(self):
        raw = b"rules:\n  - port: 8096\n"
        with mock.patch.object(provision, "_bridge_port", return_value=8096):
            self.assertEqual(provision.port_rewrite(raw), raw)

    def test_a_moved_bridge_rewrites_the_policy_the_way_install_sh_does(self):
        raw = b"rules:\n  - port: 8096\n"
        with mock.patch.object(provision, "_bridge_port", return_value=9391):
            self.assertEqual(provision.port_rewrite(raw), b"rules:\n  - port: 9391\n")

    def test_a_longer_port_is_left_alone_like_seds_word_boundary(self):
        raw = b"port: 80960\n"
        with mock.patch.object(provision, "_bridge_port", return_value=9391):
            self.assertEqual(provision.port_rewrite(raw), raw,
                             "the \\b boundary is gone, so `port: 80960` is being "
                             "corrupted the way a bare s/8096/N/ would.")


class _FakeRuntime:
    """Stands in for NemoClawRuntime with no CLI and no sandbox."""

    def __init__(self, record=None, live=True):
        self.sandbox = "test-sandbox"
        self._record = record
        self._live = live

    def registry_record(self):
        return self._record

    def live(self):
        return {"live": self._live,
                "reason": "" if self._live else "the sandbox container is not running"}


class StateTests(unittest.TestCase):
    """`state()` against a synthetic repo + registry, so no real files matter."""

    WANT = {
        "persona": [{"id": "IDENTITY.md", "label": "Persona", "sha256": _sha("p"), "rel": "x"}],
        "policies": [
            {"id": "ava-weather", "label": "ava-weather", "sha256": _sha("w"), "rel": "a"},
            {"id": "ava-email", "label": "ava-email", "sha256": _sha("e"), "rel": "b"},
        ],
        "servers": [{"id": "ava-admin", "label": "ava-admin", "sha256": _sha("s"), "rel": "c"}],
        "skills": [{"id": "weather", "label": "Weather", "sha256": _sha("k"), "rel": "d"}],
    }

    def _state(self, *, record=None, live=True, run=None, manifest=None):
        rt = _FakeRuntime(record=record, live=live)
        with mock.patch.object(provision, "desired", return_value=self.WANT), \
             mock.patch.object(provision, "_manifest_map", return_value=manifest), \
             mock.patch.object(provision, "load_run", return_value=run):
            provision.invalidate()
            return provision.state(rt=rt, force=True)

    def test_a_stopped_sandbox_reports_unknown_and_zero_pending(self):
        """The exact state a box is in when someone reloads the Setup page after
        an OOM kill. It must not read as a to-do list."""
        st = self._state(record=None, live=False)
        self.assertEqual(st["pending"], 0,
                         "a sandbox we cannot see into produced pending changes")
        for scope in provision.SCOPES:
            self.assertEqual(st["scopes"][scope]["state"], "unknown")
        self.assertFalse(st["sandbox"]["live"])
        self.assertIn("not running", st["sandbox"]["reason"])

    def test_an_applied_policy_matching_its_file_reads_deployed(self):
        record = {"customPolicies": [{"name": "ava-weather", "content": "w"},
                                     {"name": "ava-email", "content": "e"}]}
        st = self._state(record=record)
        pol = {i["id"]: i["state"] for i in st["items"] if i["scope"] == "policies"}
        self.assertEqual(pol, {"ava-weather": "deployed", "ava-email": "deployed"})
        self.assertEqual(st["scopes"]["policies"]["pending"], 0)

    def test_a_policy_the_sandbox_never_accepted_reads_undeployed(self):
        """The ava-email-read-only case: install.sh's `|| true` swallowed the
        rejection, so nothing in Ava ever reported it."""
        record = {"customPolicies": [{"name": "ava-weather", "content": "w"}]}
        st = self._state(record=record)
        pol = {i["id"]: i["state"] for i in st["items"] if i["scope"] == "policies"}
        self.assertEqual(pol["ava-email"], "undeployed")
        self.assertEqual(st["scopes"]["policies"]["pending"], 1)
        self.assertIn("policies", st["scopes_to_provision"])

    def test_an_edited_policy_reads_stale(self):
        record = {"customPolicies": [{"name": "ava-weather", "content": "OLD"},
                                     {"name": "ava-email", "content": "e"}]}
        st = self._state(record=record)
        pol = {i["id"]: i["state"] for i in st["items"] if i["scope"] == "policies"}
        self.assertEqual(pol["ava-weather"], "stale")

    def test_policies_are_readable_with_the_container_stopped(self):
        """The whole reason drift reads the registry rather than probing: this is
        real, actionable state that survives the sandbox being down."""
        record = {"customPolicies": [{"name": "ava-weather", "content": "w"}]}
        st = self._state(record=record, live=False)
        self.assertEqual(st["scopes"]["policies"]["source"], "registry")
        self.assertEqual(st["scopes"]["policies"]["pending"], 1)
        self.assertEqual(st["scopes"]["persona"]["state"], "unknown")

    def test_a_rebuild_flips_in_sandbox_items_to_undeployed_but_spares_policies(self):
        """Policies are gateway rules recorded host-side, so a wiped image does not
        remove them — reporting them as gone would send the owner to re-apply
        something that is still enforced."""
        record = {"imageTag": "img-B",
                  "customPolicies": [{"name": "ava-weather", "content": "w"},
                                     {"name": "ava-email", "content": "e"}]}
        st = self._state(record=record, run={"image_tag": "img-A"})
        self.assertTrue(st["sandbox"]["rebuilt"])
        self.assertEqual(st["scopes"]["persona"]["state"], "undeployed")
        self.assertEqual(st["scopes"]["skills"]["state"], "undeployed")
        self.assertEqual(st["scopes"]["policies"]["state"], "deployed")

    def test_an_unchanged_image_tag_is_not_a_rebuild(self):
        record = {"imageTag": "img-A", "customPolicies": []}
        st = self._state(record=record, run={"image_tag": "img-A"})
        self.assertFalse(st["sandbox"]["rebuilt"])

    def test_pending_is_stale_plus_undeployed_and_never_unknown(self):
        record = {"customPolicies": [{"name": "ava-weather", "content": "OLD"}]}
        st = self._state(record=record, live=False)
        counts = st["counts"]
        self.assertEqual(st["pending"], counts["stale"] + counts["undeployed"])
        self.assertGreater(counts["unknown"], 0, "this fixture should have unknowns")

    def test_the_skills_manifest_is_used_when_there_is_no_probe(self):
        st = self._state(manifest={"weather": _sha("k")})
        sk = {i["id"]: i["state"] for i in st["items"] if i["scope"] == "skills"}
        self.assertEqual(sk["weather"], "deployed")
        self.assertEqual(st["scopes"]["skills"]["source"], "manifest")

    def test_a_missing_manifest_is_unknown_not_undeployed(self):
        """The state this box was actually in: no skills_deployed.json at all,
        while five MCP servers and a persona were demonstrably live."""
        st = self._state(manifest=None)
        self.assertEqual(st["scopes"]["skills"]["state"], "unknown")
        self.assertEqual(st["scopes"]["skills"]["pending"], 0)


class SkillOverlayTests(unittest.TestCase):
    def test_apply_skill_states_preserves_shape_and_uses_legal_states(self):
        catalog = [{"dir": "weather", "title": "Weather", "deployed": "unknown"},
                   {"dir": "notes", "title": "Notes", "deployed": "unknown"}]
        fake = {"items": [{"scope": "skills", "id": "weather", "state": "deployed"},
                          {"scope": "skills", "id": "notes", "state": "stale"}]}
        with mock.patch.object(provision, "state", return_value=fake):
            out = provision.apply_skill_states(catalog)
        self.assertEqual([s["dir"] for s in out], ["weather", "notes"])
        self.assertEqual([s["deployed"] for s in out], ["deployed", "stale"])
        for s in out:
            self.assertIn(s["deployed"], provision.STATES)

    def test_a_broken_drift_computation_never_breaks_the_skills_list(self):
        catalog = [{"dir": "weather", "title": "Weather", "deployed": "unknown"}]
        with mock.patch.object(provision, "state", side_effect=RuntimeError("boom")):
            self.assertEqual(provision.apply_skill_states(catalog), catalog)

    def test_the_overlay_does_not_mutate_the_catalog_it_was_given(self):
        catalog = [{"dir": "weather", "title": "Weather", "deployed": "unknown"}]
        fake = {"items": [{"scope": "skills", "id": "weather", "state": "deployed"}]}
        with mock.patch.object(provision, "state", return_value=fake):
            provision.apply_skill_states(catalog)
        self.assertEqual(catalog[0]["deployed"], "unknown",
                         "apply_skill_states mutated skills.catalog()'s cached list")


class DigestContractTests(unittest.TestCase):
    def test_the_persona_digest_carries_no_trailing_newline(self):
        """install.sh pipes the prompt with `printf %s`. A newline here would put
        every install permanently one byte out and report a current persona as
        stale, forever."""
        with mock.patch.object(provision, "_persona_render", return_value="hello"):
            self.assertEqual(provision.persona_digest(), _sha("hello"))
            self.assertNotEqual(provision.persona_digest(), _sha("hello\n"))

    def test_the_sandbox_fold_reproduces_the_repo_fold(self):
        """The producer/consumer contract, end to end.

        `desired()` folds the repo directory with `tree_digest`; the runtime
        folds the sandbox mirror. install.sh does `rm -rf "$DEST"` before
        extracting `tar czf - -C "$src" .`, so the two byte sets are identical
        and the two folds must be too. Nothing checked that, which is how the
        repo's TREE digest came to be compared against the sandbox's
        ENTRY-POINT digest.
        """
        import tempfile

        from ava_bridge.runtime.nemoclaw import NemoClawRuntime

        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "apps", "acme"))
            for rel, body in (("_server.mjs", "export default 1\n"),
                              ("_lib.mjs", "// lib\n"),
                              ("apps/acme/acme_call.mjs", "// generated\n")):
                with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                    f.write(body)
            repo_fold = provision.tree_digest(d)

            # Exactly what `find <root> -type f -exec sha256sum -- {} +` prints
            # for a byte-exact mirror of that tree at a sandbox path.
            root = "/sandbox/.openclaw/mcp_server_acme"
            out = []
            for dirpath, _dirs, files in os.walk(d):
                for fn in sorted(files):
                    full = os.path.join(dirpath, fn)
                    with open(full, "rb") as f:
                        sha = hashlib.sha256(f.read()).hexdigest()
                    out.append(f"{sha}  {root}/{os.path.relpath(full, d)}")

            rt = NemoClawRuntime()
            with mock.patch.object(rt, "exec", return_value="\n".join(out) + "\n"):
                self.assertEqual(rt.tree_digests([root]), {root: repo_fold},
                                 "the sandbox fold and the repo fold disagree, so "
                                 "a correctly deployed server can never read deployed")

    def test_a_fold_that_could_not_run_is_none_not_empty(self):
        """`{}` reads as "the sandbox holds nothing" -> undeployed -> a to-do
        list. Only `None` reads as "we could not look" -> unknown."""
        from ava_bridge.runtime.base import AgentRuntime
        from ava_bridge.runtime.nemoclaw import NemoClawRuntime

        rt = NemoClawRuntime()
        with mock.patch.object(rt, "exec", return_value=""):
            self.assertIsNone(rt.tree_digests(["/sandbox/.openclaw/mcp_server_acme"]))
        # And a runtime that cannot look inside at all says so by default.
        self.assertIsNone(AgentRuntime.tree_digests(rt, ["/whatever"]))

    def test_a_server_is_folded_against_its_tree_not_its_entry_point(self):
        """The regression, asserted against the REAL shipped servers.

        `desired()` publishes a whole-tree fold while the sandbox side used to
        report the digest of `_server.mjs` alone. Folds of different byte sets
        can never be equal, so every server read `stale` in perpetuity: the
        pending count never reached zero and `provision_job`'s post-apply
        assert vetoed every run that had in fact succeeded.
        """
        rows = provision.desired()["servers"]
        self.assertTrue(rows, "no mcp_server_* dirs were discovered at all")
        for row in rows:
            self.assertEqual(row["path"], row["sandbox_root"] + "/_server.mjs",
                             "the entry point must hang off the folded root")
            entry = os.path.join(config.ROOT, row["rel"], "_server.mjs")
            with open(entry, "rb") as f:
                entry_sha = hashlib.sha256(f.read()).hexdigest()
            self.assertNotEqual(
                row["sha256"], entry_sha,
                f"{row['id']}: desired() is publishing an entry-point digest "
                "where the sandbox side folds a whole tree")

    def test_the_run_record_is_provenance_and_never_a_drift_source(self):
        """A hand-run of `cd agent && ./install.sh` leaves this stale. Drift must
        not read it for item truth — only for the imageTag rebuild baseline."""
        import inspect
        src = inspect.getsource(provision.state)
        self.assertIn("image_tag", src)
        for scope in ("persona", "policies", "servers", "skills"):
            self.assertNotIn(f'run["{scope}"]', src)
            self.assertNotIn(f"run.get(\"{scope}\")", src)


class _MirrorRuntime:
    """A sandbox that IS a byte-exact mirror of this checkout's server dirs.

    Deliberately derives its answers from the same files `desired()` reads,
    because that is the physical guarantee install.sh provides: `rm -rf "$DEST"`
    then extract `tar czf - -C "$src" .`. Nothing here is hand-fed, which is the
    point — the shipped bug survived precisely because both sides of the
    comparison were hand-fed in a fixture.
    """

    sandbox = "test-sandbox"

    def __init__(self, rows):
        self._byroot = {r["sandbox_root"]: r for r in rows}
        self._rows = rows

    def registry_record(self):
        return {}

    def live(self):
        return {"live": True, "reason": ""}

    def read_file(self, path, timeout=20):
        return json.dumps({"mcp": {"servers": {
            r["id"]: {"command": "node", "args": [r["path"]]} for r in self._rows}}})

    def digest(self, paths, timeout=30):
        return {p: _sha(p) for p in paths}   # only the witness matters here

    def glob_digest(self, pattern, timeout=30):
        return {}

    def tree_digests(self, roots, timeout=30):
        # Folds exactly what install.sh tars: the shipped server directory with
        # the generated one merged over it. Using only `rel` here would pass on
        # a plain checkout and fail the moment AVA_HOME differs — which is the
        # install shape the merge exists for.
        return {root: provision.merged_tree_digest(
            provision.server_sources(self._byroot[root]["category"]))
            for root in roots if root in self._byroot}


class RealServerDriftTests(unittest.TestCase):
    """End to end over the REAL shipped servers, with no fixture digests."""

    def test_a_byte_exact_mirror_reads_deployed(self):
        rows = provision.desired()["servers"]
        self.assertTrue(rows, "no mcp_server_* dirs were discovered at all")
        with mock.patch.object(provision, "_manifest_map", return_value=None), \
             mock.patch.object(provision, "load_run", return_value=None):
            provision.invalidate()
            st = provision.state(rt=_MirrorRuntime(rows), force=True)
        scope = st["scopes"]["servers"]
        self.assertEqual(
            scope["state"], "deployed",
            "a sandbox holding exactly what this checkout declares must read "
            "deployed. Anything else means Apply can never clear the badge, and "
            "the post-apply assert will veto runs that actually worked.")
        self.assertEqual(scope["counts"]["stale"], 0)
        self.assertEqual(scope["pending"], 0)

    def test_an_unregistered_server_still_reads_undeployed(self):
        """The mirror must not paper over presence: content is the tree fold's
        question, presence is openclaw.json's, and dropping the registration has
        to still land in `undeployed`."""
        rows = provision.desired()["servers"]
        rt = _MirrorRuntime(rows)
        rt.read_file = lambda path, timeout=20: json.dumps({"mcp": {"servers": {}}})
        with mock.patch.object(provision, "_manifest_map", return_value=None), \
             mock.patch.object(provision, "load_run", return_value=None):
            provision.invalidate()
            st = provision.state(rt=rt, force=True)
        self.assertEqual(st["scopes"]["servers"]["counts"]["undeployed"], len(rows))

    def test_a_runtime_that_cannot_fold_leaves_servers_unknown(self):
        """RemoteRuntime's case. `unknown` is not pending; `undeployed` would put
        a to-do list in front of an owner whose sandbox we simply cannot read."""
        rows = provision.desired()["servers"]
        rt = _MirrorRuntime(rows)
        rt.tree_digests = lambda roots, timeout=30: None
        with mock.patch.object(provision, "_manifest_map", return_value=None), \
             mock.patch.object(provision, "load_run", return_value=None):
            provision.invalidate()
            st = provision.state(rt=rt, force=True)
        scope = st["scopes"]["servers"]
        self.assertEqual(scope["state"], "unknown")
        self.assertEqual(scope["pending"], 0)


class RunRecordTests(unittest.TestCase):
    def test_record_run_writes_atomically_and_reloads(self):
        rt = _FakeRuntime(record={"imageTag": "img-A", "nemoclawVersion": "0.0.96"})
        written = provision.record_run(scope="all", source="test", started=1.0,
                                       ended=2.0, rc=0, rt=rt)
        self.assertEqual(written["image_tag"], "img-A")
        self.assertEqual(written["versions"]["nemoclaw"], "0.0.96")
        back = provision.load_run()
        self.assertIsNotNone(back)
        self.assertEqual(back["scope"], "all")
        # No .tmp left behind.
        import os
        self.assertFalse(os.path.exists(provision.run_record_path() + ".tmp"))

    def test_an_unreadable_record_degrades_to_none(self):
        with open(provision.run_record_path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        try:
            self.assertIsNone(provision.load_run())
        finally:
            import os
            os.remove(provision.run_record_path())

    def test_the_record_carries_no_secret_material(self):
        rt = _FakeRuntime(record={"imageTag": "img-A", "credentialEnv": "OPENAI_API_KEY"})
        written = provision.record_run(scope="all", source="test", started=1.0,
                                       ended=2.0, rc=0, rt=rt)
        blob = json.dumps(written)
        self.assertNotIn("credentialEnv", blob,
                         "the run record copied a credential field out of the "
                         "NemoClaw registry; it must carry only what drift needs.")


if __name__ == "__main__":
    unittest.main()
