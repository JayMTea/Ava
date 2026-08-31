"""The bridge and `agent/install.sh` must derive from the SAME root secret.

Each MCP server is handed a scoped token `HMAC(root, "ava-internal:<group>")`
(`agent/install.sh` §2b) and the bridge accepts it only if the same HMAC over
ITS OWN root matches (`ava_bridge/internal.py::_derived_token`). So the two sides
do not merely need the same algorithm — they need the same `root`.

`ava_bridge/config.py:333` states the assumption that makes this usually
invisible: "Both containers mount /data, so the internal token ... is the same on
both sides — no separate secret to distribute." That is true of a single-host
compose and FALSE the moment the bridge and the agent runtime are on different
machines with their own volumes.

WHAT THAT COSTS, observed on a two-host lab: the bridge held root `ec0bd285…`
and the agent host `ad9f9d0b…`. install.sh reached its `[ ! -s "$TOKEN_FILE" ]`
branch, minted its own root with `openssl rand -hex 32`, and derived five
perfectly well-formed tokens from it. The MCP servers would then START — and
every `/internal/*` callback 401s. That is quieter than a server which fails to
boot, because the only symptom is a tool that returns nothing.

`AVA_INTERNAL_TOKEN` makes the root declarable, next to the `AVA_AGENT_TOKEN` and
`AVA_ROUTER_TOKEN` such deployments already pin for exactly this reason. These
tests pin that install.sh honours it, prefers it over the file, and derives from
it — because a silently-regenerated root is indistinguishable from a correct one
until a tool call fails.

House style follows tests/test_install_scope_exec.py: run the REAL script against
a stub `nemoclaw`, assert on what it did.

Run: .venv/bin/python -m pytest tests/test_install_internal_token_root.py -q
"""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"

#: A root that is obviously not a generated one, so a test failure cannot be
#: mistaken for a coincidence.
PINNED_ROOT = "a" * 64

_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${STUB_LOG:?}"
if [ "$1" = "list" ]; then
  printf '{"sandboxes":[{"name":"my-assistant","connected":true}]}\\n'
  exit 0
fi
exit 0
"""


def derived(root: str, group: str) -> str:
    """The same HMAC both sides compute. Written out rather than imported from
    `ava_bridge.internal` on purpose: importing the implementation under test
    would make this pass even if BOTH sides changed together and diverged from
    the tokens already deployed in a live sandbox."""
    return hmac.new(root.encode(), f"ava-internal:{group}".encode(),
                    hashlib.sha256).hexdigest()


def _requirements_met() -> bool:
    return all(shutil.which(b) for b in ("bash", "tar", "base64", "python3"))


@unittest.skipUnless(_requirements_met(), "needs bash, tar, base64, python3")
class InternalTokenRootTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ava-install-root-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        stub = self.tmp / "nemoclaw"
        stub.write_text(_STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.stub = stub
        self.log = self.tmp / "calls.log"
        self.home = self.tmp / "home"
        self.token_file = self.home / "data" / ".internal_token"

    def _run(self, *args: str, env_extra: dict | None = None):
        env = {
            **os.environ,
            "STUB_LOG": str(self.log),
            "AVA_NEMOCLAW": str(self.stub),
            "AVA_HOME": str(self.home),
            "AVA_DATA_DIR": str(self.home / "data"),
            "AVA_OVERLAY": str(self.tmp / "no-overlay"),
        }
        env.pop("AVA_PROVISION_ONLY", None)
        env.pop("AVA_INTERNAL_TOKEN", None)
        env.update(env_extra or {})
        p = subprocess.run(["bash", str(AGENT / "install.sh"), *args],
                           capture_output=True, text=True, timeout=300,
                           cwd=str(AGENT), env=env)
        calls = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        return p, calls

    # -- the env var is honoured, and wins ----------------------------------
    def test_the_env_root_is_what_the_servers_are_given(self):
        """The assertion that matters: not that the variable is read, but that
        the tokens actually handed to the MCP servers derive from it."""
        p, calls = self._run("--only", "servers",
                             env_extra={"AVA_INTERNAL_TOKEN": PINNED_ROOT})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        want = derived(PINNED_ROOT, "system")
        self.assertIn(want, calls,
                      "the `system` MCP server was not given a token derived "
                      "from AVA_INTERNAL_TOKEN, so the bridge will 401 every "
                      "callback it makes")

    def test_it_does_not_mint_a_second_root_behind_your_back(self):
        """`openssl rand` here is what silently splits a two-host install. With
        the root pinned there is nothing to generate, and a file appearing is
        the tell that the env var was ignored."""
        self._run("--only", "servers",
                  env_extra={"AVA_INTERNAL_TOKEN": PINNED_ROOT})
        self.assertFalse(self.token_file.exists(),
                         "install.sh generated its own root while one was "
                         "pinned — the two hosts are now derived from "
                         "different secrets")

    def test_the_env_beats_a_file_that_disagrees(self):
        """Precedence must match `ava_bridge/config.py::_internal_token`, which
        reads AVA_INTERNAL_TOKEN FIRST. If the two orders differ, the bridge and
        the installer pick different roots from the same box."""
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text("b" * 64 + "\n", encoding="utf-8")
        _, calls = self._run("--only", "servers",
                             env_extra={"AVA_INTERNAL_TOKEN": PINNED_ROOT})
        self.assertIn(derived(PINNED_ROOT, "system"), calls)
        self.assertNotIn(derived("b" * 64, "system"), calls,
                         "the file won over the env var, which inverts the "
                         "bridge's own precedence")

    def test_the_pinned_root_itself_is_never_handed_out(self):
        """Servers get SCOPED tokens. Passing the root would give the web-fetch
        server — the surface prompt injection arrives on — full `/internal/*`
        access, which is the escalation the group scoping exists to stop."""
        _, calls = self._run("--only", "servers",
                             env_extra={"AVA_INTERNAL_TOKEN": PINNED_ROOT})
        self.assertNotIn(PINNED_ROOT, calls,
                         "the ROOT secret was passed to an MCP server")

    # -- and the unpinned path still works ----------------------------------
    def test_without_the_env_it_still_uses_the_file(self):
        """The single-host default must not regress: no env var, a file already
        there, and that file's root is what gets derived from."""
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text("c" * 64 + "\n", encoding="utf-8")
        p, calls = self._run("--only", "servers")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(derived("c" * 64, "system"), calls)

    def test_without_either_it_generates_one_and_keeps_it(self):
        """A fresh single-host install has no secret yet; minting one is correct
        there, and it must be persisted or every run derives differently."""
        p, _ = self._run("--only", "servers")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(self.token_file.exists())
        self.assertEqual(len(self.token_file.read_text(encoding="utf-8").strip()), 64)


if __name__ == "__main__":
    unittest.main()
