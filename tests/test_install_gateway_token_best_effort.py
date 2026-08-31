"""A gateway token nobody can mint must not cost the owner every Ava tool.

WHAT THIS PREVENTS. `agent/install.sh` runs under `set -euo pipefail` (line 38).
Its §2b-ii block fetches OpenClaw's gateway token and is documented, in its own
comment, as best-effort: "A missing token must never fail an install whose actual
job is deploying tools and policies." That promise was made in prose and not in
the shell. With `pipefail` the pipeline takes `nemoclaw`'s non-zero status, an
assignment from a command substitution propagates it, and `set -e` killed the
script right there — BEFORE §2c discovered a single MCP server and §3 deployed
one.

Observed on a real two-host install: `nemoclaw <sandbox> gateway-token` exits 1
("Could not retrieve the gateway auth token"), so install.sh aborted silently
every run. The five `ava-*` MCP servers kept the argv a NemoClaw migration had
left them — the literal string `[STRIPPED_BY_MIGRATION]` — and every one failed
to start, every thirty minutes, for days. The only symptom above the log line was
`[agent] WARNING: install.sh reported issues` and an agent with zero Ava tools.

The sibling `dashboard-url` fallback four lines down was already `|| true`
guarded, which is exactly what made the gap invisible to review.

House style follows tests/test_install_scope_exec.py: run the REAL script against
a stub `nemoclaw`, assert on what it did. Nothing here touches a real sandbox,
the real CLI, or a real data directory.

Run: .venv/bin/python -m pytest tests/test_install_gateway_token_best_effort.py -q
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

#: A stub that reproduces the box this bug was found on: every subcommand works
#: EXCEPT `gateway-token`, which fails the way the real CLI fails there. The
#: `list --json` answer satisfies install.sh's §0 bootstrap guard.
_STUB_TOKEN_FAILS = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${STUB_LOG:?}"
if [ "$1" = "list" ]; then
  printf '{"sandboxes":[{"name":"my-assistant","connected":true}]}\\n'
  exit 0
fi
for a in "$@"; do
  case "$a" in
    gateway-token|dashboard-url)
      echo "Could not retrieve the gateway auth token for sandbox 'my-assistant'." >&2
      exit 1 ;;
  esac
done
exit 0
"""

#: The same stub, but the CLI can mint a token — the happy path, so the test
#: below proves the guard did not simply neuter the feature.
_STUB_TOKEN_WORKS = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${STUB_LOG:?}"
if [ "$1" = "list" ]; then
  printf '{"sandboxes":[{"name":"my-assistant","connected":true}]}\\n'
  exit 0
fi
for a in "$@"; do
  case "$a" in
    gateway-token) printf 'tok-abc123\\n'; exit 0 ;;
  esac
done
exit 0
"""


def _requirements_met() -> bool:
    return all(shutil.which(b) for b in ("bash", "tar", "base64", "python3"))


@unittest.skipUnless(_requirements_met(), "needs bash, tar, base64, python3")
class GatewayTokenIsBestEffort(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ava-install-gwtok-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "calls.log"

    def _run(self, stub_body: str, *args: str):
        stub = self.tmp / "nemoclaw"
        stub.write_text(stub_body, encoding="utf-8")
        stub.chmod(0o755)
        env = {
            **os.environ,
            "STUB_LOG": str(self.log),
            "AVA_NEMOCLAW": str(stub),
            "AVA_HOME": str(self.tmp / "home"),
            "AVA_DATA_DIR": str(self.tmp / "home" / "data"),
            # Point the overlay at nothing so a developer's private overlay
            # cannot change what this asserts on.
            "AVA_OVERLAY": str(self.tmp / "no-overlay"),
        }
        env.pop("AVA_PROVISION_ONLY", None)
        p = subprocess.run(["bash", str(AGENT / "install.sh"), *args],
                           capture_output=True, text=True, timeout=300,
                           cwd=str(AGENT), env=env)
        return p, (p.stdout or "") + (p.stderr or "")

    # -- the regression itself ----------------------------------------------
    def test_a_cli_that_cannot_mint_a_token_does_not_abort_the_install(self):
        p, out = self._run(_STUB_TOKEN_FAILS, "--only", "servers")
        self.assertEqual(
            p.returncode, 0,
            "install.sh died because the gateway token could not be minted. That "
            "block is best-effort by its own comment, and the whole point of the "
            "run — deploying the MCP servers — happens AFTER it.\n" + out)

    def test_it_still_reaches_the_mcp_servers(self):
        """The assertion that actually matters. Exit 0 alone would not prove the
        script got past §2b-ii — only that it did not shout on the way out."""
        p, out = self._run(_STUB_TOKEN_FAILS, "--only", "servers")
        self.assertIn(
            "mcp server(s)", out,
            "§2c never ran, so no MCP server was discovered and none was "
            "deployed — which is precisely how five servers kept a placeholder "
            "token for days while the install reported only 'issues'.\n" + out)

    def test_it_reports_the_skip_rather_than_staying_silent(self):
        """`_step gateway token skip` is the line that tells an operator the
        token is missing. An abort produces no step line at all, which is why
        the failure presented as a generic warning with nothing to grep for."""
        _, out = self._run(_STUB_TOKEN_FAILS, "--only", "servers")
        self.assertIn("gateway\ttoken\tskip", out,
                      "the skip branch never executed\n" + out)

    def test_no_secret_file_is_written_when_there_is_no_token(self):
        """The guard must not turn a failure into a file full of an error
        message: `settings.secret(...)` is called with generate=False precisely
        so a bogus value cannot masquerade as a real credential."""
        self._run(_STUB_TOKEN_FAILS, "--only", "servers")
        self.assertFalse((self.tmp / "home" / "secrets"
                          / "openclaw_gateway_token").exists())

    # -- and the guard did not neuter the feature ----------------------------
    def test_a_working_cli_still_writes_the_token(self):
        _, out = self._run(_STUB_TOKEN_WORKS, "--only", "servers")
        path = self.tmp / "home" / "secrets" / "openclaw_gateway_token"
        self.assertTrue(path.exists(),
                        "`|| true` swallowed the SUCCESS path too\n" + out)
        self.assertEqual(path.read_text(encoding="utf-8").strip(), "tok-abc123")
        self.assertIn("gateway\ttoken\tok", out)

    def test_the_secret_is_written_owner_only(self):
        """It is a credential; 0600 is the same discipline data/.internal_token
        gets four lines up."""
        if os.name == "nt":
            self.skipTest("POSIX mode bits")
        self._run(_STUB_TOKEN_WORKS, "--only", "servers")
        path = self.tmp / "home" / "secrets" / "openclaw_gateway_token"
        self.assertEqual(path.stat().st_mode & 0o077, 0,
                         "the gateway token is group/world readable")


if __name__ == "__main__":
    unittest.main()
