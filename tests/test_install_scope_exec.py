"""Run `agent/install.sh` for real, against a stub `nemoclaw` that records argv.

`test_provision_scope_matrix.py` asserts on the *plan* (`--dry-run`); this asserts
on the *calls*. Both exist because they fail differently: a plan can be right
while the gating below it is wrong, and a static grep can pass while both are.

The stub answers `list --json` so the §0 bootstrap guard is satisfied, and no-ops
everything else. Nothing here touches the real sandbox, the real CLI, or the real
data directory — which matters more than usual, because `nemoclaw <sandbox> status
--json` was observed restarting the host's OpenShell gateway and killing a host
process. No test may ever invoke the real binary.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"

_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${STUB_LOG:?}"
if [ "$1" = "list" ]; then
  printf '{"sandboxes":[{"name":"my-assistant","connected":true}]}\\n'
  exit 0
fi
exit 0
"""


def _requirements_met() -> bool:
    return all(shutil.which(b) for b in ("bash", "tar", "base64", "python3"))


@unittest.skipUnless(_requirements_met(), "needs bash, tar, base64, python3")
class ScopedInstallTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ava-install-scope-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        stub = self.tmp / "nemoclaw"
        stub.write_text(_STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.stub = stub
        self.log = self.tmp / "calls.log"

    def _run(self, *args: str, env_extra: dict | None = None):
        env = {
            **os.environ,
            "STUB_LOG": str(self.log),
            "AVA_NEMOCLAW": str(self.stub),
            "AVA_HOME": str(self.tmp / "home"),
            "AVA_DATA_DIR": str(self.tmp / "home" / "data"),
            # Point the overlay at nothing so a developer's private overlay does
            # not change the counts this asserts on.
            "AVA_OVERLAY": str(self.tmp / "no-overlay"),
            **(env_extra or {}),
        }
        env.pop("AVA_PROVISION_ONLY", None)
        p = subprocess.run(["bash", str(AGENT / "install.sh"), *args],
                           capture_output=True, text=True, timeout=300,
                           cwd=str(AGENT), env=env)
        calls = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return p, calls

    @staticmethod
    def _count(calls: list[str], needle: str) -> int:
        return sum(1 for c in calls if needle in c)

    @staticmethod
    def _register_call(calls: list[str]) -> str:
        return next((c for c in calls if "node -e" in c), "")

    def test_a_full_run_does_every_kind_of_work(self):
        p, calls = self._run()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertGreater(self._count(calls, "policy-add"), 0)
        self.assertGreater(self._count(calls, "skill install"), 0)
        self.assertGreater(self._count(calls, "env DEST="), 0)
        self.assertEqual(self._count(calls, "node -e"), 1)

    def test_persona_only_never_unregisters_the_mcp_servers(self):
        """The regression this whole increment exists to prevent.

        If SPECS_JSON is ever built inside the scope-gated deploy loop, this call
        carries `AVA_SERVERS=[]`, §4/5 deletes every ava-* key and registers
        nothing, and the script still exits 0.
        """
        full_p, full_calls = self._run()
        self.assertEqual(full_p.returncode, 0, full_p.stdout + full_p.stderr)
        full_names = self._register_call(full_calls).count("ava-")
        self.assertGreater(full_names, 0, "the full run registered no servers")

        self.log.write_text("", encoding="utf-8")
        p, calls = self._run("--only", "persona")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        register = self._register_call(calls)
        self.assertTrue(register, "a persona-only run did not register servers at all")
        # SPECS_JSON is the last positional, so an empty set shows up as a bare
        # trailing `[]`. Verified by reintroducing the bug against a throwaway
        # copy: the run still exits 0 and logs green, and the ONLY visible
        # difference is right here.
        self.assertFalse(register.rstrip().endswith(" []"),
                         "the registration call carries an EMPTY server list — this "
                         "unregisters every ava-* MCP server in the sandbox.")
        self.assertEqual(register.count("ava-"), full_names,
                         "a persona-only run registers fewer servers than a full "
                         "run, so it is dropping some of them.")

    def test_persona_only_skips_policies_servers_and_skills(self):
        p, calls = self._run("--only", "persona")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self._count(calls, "policy-add"), 0)
        self.assertEqual(self._count(calls, "skill install"), 0)
        self.assertEqual(self._count(calls, "env DEST="), 0)
        self.assertEqual(self._count(calls, " recover"), 0)

    def test_policies_only_applies_policies_and_nothing_else(self):
        p, calls = self._run("--only", "policies")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertGreater(self._count(calls, "policy-add"), 0)
        self.assertEqual(self._count(calls, "skill install"), 0)
        self.assertEqual(self._count(calls, "env DEST="), 0)
        self.assertEqual(self._count(calls, "node -e"), 0,
                         "a policy-only run touched openclaw.json, which is where "
                         "the server registration lives")

    def test_skills_only_installs_skills_and_nothing_else(self):
        p, calls = self._run("--only", "skills")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertGreater(self._count(calls, "skill install"), 0)
        self.assertEqual(self._count(calls, "policy-add"), 0)
        self.assertEqual(self._count(calls, "node -e"), 0)

    def test_an_unknown_scope_invokes_nothing_at_all(self):
        p, calls = self._run("--only", "bogus")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(calls, [],
                         "an invalid scope still ran commands against the sandbox")

    def test_a_stopped_sandbox_does_not_abort_the_install_silently(self):
        """§2 reads HTTPS_PROXY out of the sandbox. The stub returns nothing —
        exactly what a stopped container does — and `grep -oE` exits 1 on
        no-match. Under pipefail that used to kill the run one line before the
        documented fallback, with stderr discarded and grep printing nothing, so
        it died in complete silence after applying the policies.
        """
        p, _ = self._run("--only", "policies")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("proxy=", p.stdout,
                      "the proxy fallback did not run; §2 aborted the install")

    def test_a_skipped_scope_says_so_rather_than_appearing_to_have_worked(self):
        p, _ = self._run("--only", "persona")
        out = p.stdout + p.stderr
        self.assertIn("skipping", out,
                      "a scoped run is silent about what it did not do, which reads "
                      "as a full deploy in the log")

    def _seed_manifest(self, *names: str) -> None:
        """Write a deploy manifest as if a previous run had installed `names`."""
        data = self.tmp / "home" / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / "skills_deployed.json").write_text(
            json.dumps([{"name": n, "sha256": "0" * 64} for n in names]),
            encoding="utf-8")

    def test_a_skill_dropped_from_the_repo_is_removed_from_the_sandbox(self):
        """`skill install` is additive and nemoclaw has no `skill list`, so
        without an explicit prune a retired skill stays live in the sandbox for
        the rest of the install's life — still loaded, still claiming a
        capability the code no longer implements. The previous run's manifest is
        the only record of what is in there, so it is what the prune diffs.
        """
        self._seed_manifest("ava-web", "ava-retired")
        p, calls = self._run()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self._count(calls, "skill remove ava-retired"), 1,
                         "a skill deleted from the repo was left in the sandbox")
        self.assertEqual(self._count(calls, "skill remove ava-web"), 0,
                         "a skill the repo still ships was removed")

    def test_the_prune_survives_a_manifest_that_is_missing_or_unreadable(self):
        """A first install has no manifest, and a half-finished run can leave a
        truncated one. Neither may abort the deploy: worst case nothing is pruned.
        """
        for content in (None, "", "not json{", "{}", "[3]"):
            with self.subTest(manifest=content):
                self.log.write_text("", encoding="utf-8")
                data = self.tmp / "home" / "data"
                data.mkdir(parents=True, exist_ok=True)
                path = data / "skills_deployed.json"
                path.unlink(missing_ok=True)
                if content is not None:
                    path.write_text(content, encoding="utf-8")
                p, calls = self._run()
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(self._count(calls, "skill remove"), 0)
                self.assertGreater(self._count(calls, "skill install"), 0,
                                   "the prune stopped the install from running")

    def test_the_run_records_what_it_deployed_so_the_next_one_can_prune(self):
        self._seed_manifest("ava-retired")
        p, _ = self._run()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        written = json.loads(
            (self.tmp / "home" / "data" / "skills_deployed.json").read_text(encoding="utf-8"))
        names = {row["name"] for row in written}
        self.assertNotIn("ava-retired", names,
                         "the retired skill stayed in the manifest, so the next run "
                         "would try to remove it again forever")
        self.assertIn("ava-web", names)

    # ---- generated material under the agent state root ---------------------
    # `ava connector policies|tools --write` renders into AVA_HOME, not the
    # checkout: those files are state, and on the primary install the checkout
    # is an image layer that a rebuild throws away. install.sh has to pick them
    # up from there, and for the SERVERS that means a merge rather than a second
    # discovered root — §2c requires a `_server.mjs`, which a generated tree does
    # not have, so a second root would simply be skipped and the connector tools
    # would never ship at all.

    def _tar_members(self, calls: list[str], category: str) -> list[str]:
        """Names inside the tarball install.sh pushed for one server.

        The stub logs the whole argv on one line and the base64 payload is the
        last token, which is what makes this assertable without a sandbox.
        """
        import base64
        import gzip
        import io
        import tarfile

        marker = f"DEST=/sandbox/.openclaw/mcp_server_{category}"
        line = next((c for c in calls if marker in c), "")
        self.assertTrue(line, f"no byte push for mcp_server_{category}")
        blob = line.split()[-1]
        raw = gzip.decompress(base64.b64decode(blob))
        with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
            return tf.getnames()

    def _seed_generated(self) -> None:
        state = self.tmp / "home" / "agent"
        tools = state / "mcp_server_connectors" / "apps" / "acme"
        tools.mkdir(parents=True)
        (tools / "acme_call.mjs").write_text("// generated\n", encoding="utf-8")
        pol = state / "policies" / "generated"
        pol.mkdir(parents=True)
        (pol / "acme.yaml").write_text(
            "preset:\n  name: ava-acme\n  description: acme\n", encoding="utf-8")

    def test_generated_tools_are_merged_onto_the_shipped_server(self):
        self._seed_generated()
        p, calls = self._run()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

        members = self._tar_members(calls, "connectors")
        self.assertIn("./apps/acme/acme_call.mjs", members,
                      "the generated tool never reached the sandbox — a connector "
                      "the owner added is invisible to the agent")
        self.assertIn("./_server.mjs", members,
                      "the generated tree REPLACED the shipped server instead of "
                      "merging onto it, so the server cannot even start")

    def test_generated_policies_are_applied_from_the_state_root(self):
        self._seed_generated()
        p, calls = self._run()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(
            any("policy-add" in c and "acme" in c for c in calls),
            "the connector's egress policy was never applied, so the sandbox's "
            "deny-by-default blocks every call its tools make")

    def test_a_server_with_no_generated_material_is_pushed_unchanged(self):
        """The merge must not disturb the other four servers."""
        self._seed_generated()
        p, calls = self._run()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        members = self._tar_members(calls, "system")
        self.assertIn("./_server.mjs", members)
        self.assertFalse([m for m in members if m.startswith("./apps/")])

    def test_a_state_root_equal_to_the_checkout_is_not_applied_twice(self):
        """A plain checkout, where AVA_HOME *is* the code root: `$STATE` and
        `$HERE` are the same directory. Applying every policy twice would double
        each log line and make a rejected one twice as easy to miss, and copying
        the server tree onto itself is not a no-op."""
        p_base, calls_base = self._run()
        self.assertEqual(p_base.returncode, 0, p_base.stdout + p_base.stderr)
        baseline = self._count(calls_base, "policy-add")

        self.log.unlink()
        p, calls = self._run("--only", "policies", env_extra={
            "AVA_AGENT_STATE_DIR": str(AGENT)})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self._count(calls, "policy-add"), baseline,
                         "the same policy directory was walked twice")


if __name__ == "__main__":
    unittest.main()
