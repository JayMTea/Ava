"""`install.sh --only <scope>` must skip work without skipping *correctness*.

The dangerous scope is `persona`, and the danger is not obvious. §4/5 registers
MCP servers by DELETE-THEN-RECREATE: it drops every `/^ava-/` key from
`d.mcp.servers` and rebuilds the set from `SPECS_JSON`. So a scope that skips the
server work must still compute and pass the full server list — otherwise a
persona-only apply silently unregisters every Ava tool server, including the
overlay's private ones, while exiting 0 with a green log.

A static grep can be satisfied by code that still does the wrong thing, so these
assert on `--dry-run`, which prints the plan the script will actually follow.

House style: static/behavioural scan, no bridge, no sandbox, no network. The
dry-run path deliberately needs no nemoclaw CLI, which is what lets it run in CI.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "agent" / "install.sh"

SCOPES = ("persona", "policies", "servers", "skills")


def _plan(*args: str) -> tuple[int, str]:
    p = subprocess.run(
        ["bash", str(INSTALL_SH), *args, "--dry-run"],
        capture_output=True, text=True, timeout=60,
        cwd=str(ROOT / "agent"),
    )
    return p.returncode, p.stdout + p.stderr


@unittest.skipIf(shutil.which("bash") is None, "bash not available")
class ScopeMatrixTests(unittest.TestCase):

    def test_a_dry_run_needs_no_cli_and_touches_nothing(self):
        rc, out = _plan("--only", "all")
        self.assertEqual(rc, 0, out)
        self.assertIn("plan: scope=all", out)

    def test_persona_scope_still_registers_every_discovered_server(self):
        """THE test. `servers=0` here means a persona-only apply would wipe the
        sandbox's entire MCP server registration."""
        rc, out = _plan("--only", "persona")
        self.assertEqual(rc, 0, out)
        discovered = int(out.split("servers discovered:")[1].split()[0])
        self.assertGreater(discovered, 0, "no mcp_server_* dirs discovered at all")
        self.assertIn(f"register servers={discovered}", out,
                      "a persona-only apply does not plan to re-register every "
                      "discovered MCP server. §4/5 deletes every ava-* key before "
                      "it registers, so anything less than the full set is a "
                      "silent unregistration.\n" + out)

    def test_persona_scope_skips_the_expensive_work(self):
        rc, out = _plan("--only", "persona")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("§1 apply egress policies", out)
        self.assertNotIn("§3 push MCP server bytes", out)
        self.assertNotIn("§6 install skills", out)

    def test_each_scope_plans_only_its_own_section(self):
        expect = {
            "policies": "§1 apply egress policies",
            "servers": "§3 push MCP server bytes",
            "skills": "§6 install skills",
        }
        for scope, marker in expect.items():
            rc, out = _plan("--only", scope)
            self.assertEqual(rc, 0, out)
            self.assertIn(marker, out, f"--only {scope} does not plan its own work")
            for other, other_marker in expect.items():
                if other != scope:
                    self.assertNotIn(other_marker, out,
                                     f"--only {scope} also plans {other}'s work")

    def test_every_scoped_plan_is_a_subset_of_the_full_plan(self):
        _, full = _plan("--only", "all")
        full_sections = {ln for ln in full.splitlines() if ln.startswith("[ava]   §")}
        for scope in SCOPES:
            _, out = _plan("--only", scope)
            sections = {ln for ln in out.splitlines() if ln.startswith("[ava]   §")}
            self.assertTrue(sections <= full_sections,
                            f"--only {scope} plans work that `all` does not: "
                            f"{sections - full_sections}")

    def test_a_comma_list_unions_the_scopes(self):
        rc, out = _plan("--only", "policies,skills")
        self.assertEqual(rc, 0, out)
        self.assertIn("§1 apply egress policies", out)
        self.assertIn("§6 install skills", out)
        self.assertNotIn("§3 push MCP server bytes", out)

    def test_an_unknown_scope_fails_loudly_instead_of_doing_everything(self):
        """A future scope name reaching an older copy of this script must abort.

        Silently falling through to `all` is the shell-level version of the bug
        the remote runtime's /healthz capability handshake exists to prevent:
        asked for a small thing, quietly do the big one and report success.
        """
        rc, out = _plan("--only", "bogus")
        self.assertEqual(rc, 2, out)
        self.assertIn("unknown scope", out)

    def test_an_unknown_flag_fails_loudly(self):
        p = subprocess.run(["bash", str(INSTALL_SH), "--wat"],
                           capture_output=True, text=True, timeout=60,
                           cwd=str(ROOT / "agent"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("unknown flag", p.stdout + p.stderr)

    def test_a_connector_run_plans_one_server_and_one_policy(self):
        """Connecting an app shipped two generated files and cost a full deploy
        of everything, because `deploy_connector` had no narrower verb to ask
        for. The plan is where that narrowing has to be visible — it is what a
        human reads before letting it run."""
        rc, out = _plan("--only", "policies,servers", "--connector", "acme")
        self.assertEqual(rc, 0, out)
        self.assertIn("connector=acme", out)
        self.assertIn("§1 apply egress policies (only acme)", out)
        self.assertIn("§3 push MCP server bytes (only mcp_server_connectors)", out)
        self.assertNotIn("§6 install skills", out)
        self.assertNotIn("§4/5 write persona", out,
                         "a connector run plans to write the persona, which "
                         "clears a pending count nobody pressed Apply for")
        # Registration still covers every server: §4/5 is delete-then-recreate,
        # so a subset would unregister the rest.
        discovered = int(out.split("servers discovered:")[1].split()[0])
        self.assertIn(f"register servers={discovered}", out)

    def test_registration_and_the_persona_write_are_separate_lines(self):
        """They are two facts under one `if`: registration runs for `servers`
        too, the persona write runs for `persona` alone. One line saying
        "+ write persona" claimed work a servers-only run should not do."""
        _, servers = _plan("--only", "servers")
        self.assertIn("register servers=", servers)
        self.assertNotIn("write persona", servers)
        _, persona = _plan("--only", "persona")
        self.assertIn("register servers=", persona)
        self.assertIn("write persona", persona)

    def test_the_documented_scopes_match_the_ones_the_script_accepts(self):
        """Usage text, the validator, and ava_bridge/provision.py must agree."""
        from ava_bridge import provision
        sh = INSTALL_SH.read_text(encoding="utf-8")
        declared = next(ln for ln in sh.splitlines() if ln.startswith("SCOPES_ALL="))
        declared_set = set(declared.split("=", 1)[1].strip('"').split())
        self.assertEqual(declared_set, set(provision.SCOPES),
                         "agent/install.sh and ava_bridge/provision.py disagree about "
                         "the scope vocabulary; they are the same names in the CLI, "
                         "the API and the UI.")
        usage = next(ln for ln in sh.splitlines() if "usage:" in ln)
        for scope in provision.SCOPES:
            self.assertIn(scope, usage, f"--only {scope} is undocumented in the usage line")

    def test_the_connector_flag_is_documented(self):
        """`--connector` deliberately is NOT a scope: the four scope names are
        one vocabulary shared with provision.py, the API and the UI, and a
        compound token would have to be taught to every one of them. It is a
        narrowing modifier, and the usage block has to say so or the only way to
        find it is to read the argument parser."""
        sh = INSTALL_SH.read_text(encoding="utf-8")
        head = sh.split("set -euo pipefail", 1)[0]
        self.assertIn("--connector", head, "--connector is undocumented")
        self.assertIn("--connector", sh.split("usage:", 1)[1].split("\n\n", 1)[0])
        from ava_bridge import provision
        self.assertNotIn("connector", provision.SCOPES,
                         "--connector became a scope; it must stay a modifier, or "
                         "provision.py, provision_job, the CLI, the remote "
                         "capability handshake and the Hub all have to learn it.")


if __name__ == "__main__":
    unittest.main()
