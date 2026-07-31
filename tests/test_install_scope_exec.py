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

    def _run(self, *args: str):
        env = {
            **os.environ,
            "STUB_LOG": str(self.log),
            "AVA_NEMOCLAW": str(self.stub),
            "AVA_HOME": str(self.tmp / "home"),
            "AVA_DATA_DIR": str(self.tmp / "home" / "data"),
            # Point the overlay at nothing so a developer's private overlay does
            # not change the counts this asserts on.
            "AVA_OVERLAY": str(self.tmp / "no-overlay"),
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


if __name__ == "__main__":
    unittest.main()
