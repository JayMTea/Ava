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

        # Scan the WHOLE log from the marker forward, not one line: the deploy
        # command is multi-line now (stage -> validate -> swap), so the stub's
        # `printf '%s\n' "$*"` spreads a single call over several lines and the
        # payload is no longer the last token of the line that names DEST.
        marker = f"DEST=/sandbox/.openclaw/mcp_server_{category}"
        text = "\n".join(calls)
        at = text.find(marker)
        self.assertGreater(at, -1, f"no byte push for mcp_server_{category}")
        raw = None
        for tok in text[at:].split():
            if len(tok) < 64:
                continue
            try:
                raw = gzip.decompress(base64.b64decode(tok))
                break
            except Exception:  # not the payload, keep looking
                continue
        self.assertIsNotNone(raw, "no gzip payload followed the DEST marker")
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


    # ---- the deploy is staged, validated, then swapped ---------------------
    # `rm -rf "$DEST"; mkdir -p "$DEST"; tar xzf` destroyed the running server
    # before its replacement existed, and `node --check` ran AFTER that — so a
    # syntactically broken push replaced a working server and printed a WARNING
    # while the sandbox quietly lost those tools. There was no rollback because
    # there was nothing left to roll back to.

    def _deploy_cmd(self, calls: list[str]) -> str:
        text = "\n".join(calls)
        at = text.find("DEST=/sandbox/.openclaw/mcp_server_")
        self.assertGreater(at, -1, "no server byte push happened")
        return text[at:at + 2000]

    def test_the_live_server_is_not_removed_before_its_replacement_exists(self):
        _p, calls = self._run()
        cmd = self._deploy_cmd(calls)
        self.assertNotIn('rm -rf "$DEST";', cmd,
                         "the live server directory is deleted before the new "
                         "bytes have landed")
        self.assertIn('"$DEST.new"', cmd, "the payload is not staged")

    def test_every_module_is_checked_not_just_the_entry_point(self):
        """`_server.mjs` discovers tools by recursive import and CATCHES a module
        that will not load — one line to its own stderr, then it carries on. So a
        syntactically broken tool deployed cleanly, reported success, and simply
        did not exist as far as Ava was concerned, with nothing between the
        generator and the missing tool ever saying a word."""
        self._seed_generated()
        _p, calls = self._run()
        # The CONNECTORS push specifically — the generated tool only exists
        # there, and `_deploy_cmd` returns whichever server sorts first.
        text = "\n".join(calls)
        at = text.find("DEST=/sandbox/.openclaw/mcp_server_connectors")
        self.assertGreater(at, -1, "no byte push for the connectors server")
        cmd = text[at:at + 3000]
        self.assertIn("AVA_MJS=", cmd,
                      "the module list never reaches the sandbox, so only the "
                      "entry point is checked")
        self.assertIn("apps/acme/acme_call.mjs", cmd,
                      "a generated connector tool is not in the list the deploy "
                      "checks — a syntax error in it would deploy clean and the "
                      "tool would simply not exist")
        self.assertIn("_server.mjs", cmd)

    def test_a_broken_module_keeps_the_previous_copy(self):
        """The deploy command itself, executed for real against a temp DEST —
        no sandbox, no CLI, just bash and node. Asserting the command CONTAINS a
        check only proves it was written; this proves it works, and that the
        rollback the swap exists for actually happens.
        """
        if not shutil.which("node"):
            self.skipTest("needs node to run the syntax gate")
        _p, calls = self._run()
        text = "\n".join(calls)
        at = text.find("bash -c set -eo pipefail")
        self.assertGreater(at, -1, "no deploy command was issued")
        # The command body is everything from `set -eo pipefail` to the payload.
        body = text[text.index("set -eo pipefail", at):]
        body = body[:body.index("echo \"[ava] ok: $NAME\"") + len('echo "[ava] ok: $NAME"')]

        root = self.tmp / "swap"
        dest = root / "server"
        (dest).mkdir(parents=True)
        (dest / "_server.mjs").write_text("// the GOOD copy\n", encoding="utf-8")

        # A payload whose entry point is fine and whose TOOL is broken — the
        # exact shape the entry-point-only check waved through.
        payload = self.tmp / "payload"
        (payload / "apps" / "acme").mkdir(parents=True)
        (payload / "_server.mjs").write_text("export const ok = 1\n", encoding="utf-8")
        (payload / "apps" / "acme" / "bad.mjs").write_text(
            "export default { name: 'x', handler: (\n", encoding="utf-8")
        import base64 as _b64
        import io as _io
        import tarfile as _tar
        buf = _io.BytesIO()
        with _tar.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(str(payload), arcname=".")
        b64 = _b64.b64encode(buf.getvalue()).decode()

        r = subprocess.run(
            ["bash", "-c", body, b64],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "DEST": str(dest), "NAME": "ava-tools-connectors",
                 "AVA_MJS": "_server.mjs apps/acme/bad.mjs"})

        self.assertNotEqual(r.returncode, 0,
                            "a syntactically broken tool deployed cleanly")
        self.assertIn("SYNTAX ERROR in apps/acme/bad.mjs", r.stdout + r.stderr)
        self.assertEqual((dest / "_server.mjs").read_text(), "// the GOOD copy\n",
                         "the previous copy was replaced by a payload that does "
                         "not parse — the swap's whole purpose is that it is not")
        self.assertFalse((dest / "apps").exists(), "the bad payload landed anyway")

    def test_a_sound_payload_still_swaps_in(self):
        """The other half: the gate must not block a good deploy."""
        if not shutil.which("node"):
            self.skipTest("needs node to run the syntax gate")
        _p, calls = self._run()
        text = "\n".join(calls)
        at = text.find("bash -c set -eo pipefail")
        body = text[text.index("set -eo pipefail", at):]
        body = body[:body.index("echo \"[ava] ok: $NAME\"") + len('echo "[ava] ok: $NAME"')]

        dest = self.tmp / "swap2" / "server"
        dest.mkdir(parents=True)
        (dest / "_server.mjs").write_text("// old\n", encoding="utf-8")
        payload = self.tmp / "payload2"
        (payload / "apps" / "acme").mkdir(parents=True)
        (payload / "_server.mjs").write_text("export const ok = 2\n", encoding="utf-8")
        (payload / "apps" / "acme" / "good.mjs").write_text(
            "export default { name: 'x' }\n", encoding="utf-8")
        import base64 as _b64
        import io as _io
        import tarfile as _tar
        buf = _io.BytesIO()
        with _tar.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(str(payload), arcname=".")

        r = subprocess.run(
            ["bash", "-c", body, _b64.b64encode(buf.getvalue()).decode()],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "DEST": str(dest), "NAME": "ava-tools-connectors",
                 "AVA_MJS": "_server.mjs apps/acme/good.mjs"})

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((dest / "_server.mjs").read_text(), "export const ok = 2\n")
        self.assertTrue((dest / "apps" / "acme" / "good.mjs").exists())

    def test_the_syntax_check_gates_the_swap(self):
        _p, calls = self._run()
        cmd = self._deploy_cmd(calls)
        check = cmd.index("node --check")
        swap = cmd.index('mv "$DEST.new" "$DEST"')
        self.assertLess(check, swap,
                        "a broken server is moved into place and only then "
                        "checked, so the check cannot protect anything")

    def test_a_failed_push_does_not_abort_the_rest_of_the_deploy(self):
        """A bare `_run_cli` under `set -e` aborted the whole script, so one
        server failing to push skipped registration, the persona and every
        skill — the half-installed runtime §0 exists to prevent, from the other
        end."""
        sh = (AGENT / "install.sh").read_text(encoding="utf-8")
        deploy = sh[sh.index("# --- 3. Deploy MCP servers"):sh.index("# --- 4 & 5.")]
        self.assertIn("SERVER_FAILED", deploy,
                      "a failed server push is not counted")
        self.assertIn("_step servers", deploy)
        self.assertIn("fail", deploy,
                      "the step channel reports `ok` no matter what happened")

    def test_every_policy_failing_is_not_a_zero_exit(self):
        """One rejected policy stays non-fatal on purpose. EVERY policy failing
        is the gateway rejecting the whole egress configuration, and the script
        exited 0 on it — so `provision()` recorded a green deploy step and only
        the post-hoc verify veto noticed."""
        sh = (AGENT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("POLICY_TOTAL", sh)
        self.assertRegex(sh, r'POLICY_FAILED"?\s*-eq\s*"?\$POLICY_TOTAL')
    # --- --connector: deploy ONE app without redeploying the kit ------------- #
    # Connecting an app shipped two generated files and cost a full deploy of
    # everything — five server pushes, six skill installs, seven policy applies —
    # because `deploy_connector` had no narrower verb to ask for.

    def test_a_connector_run_pushes_one_server_not_five(self):
        self._seed_generated()
        full_p, full_calls = self._run()
        self.assertEqual(full_p.returncode, 0, full_p.stdout + full_p.stderr)
        full_pushes = self._count(full_calls, "env DEST=")
        self.assertGreater(full_pushes, 1, "the full run pushed one server or none")

        self.log.write_text("", encoding="utf-8")
        p, calls = self._run("--only", "policies,servers", "--connector", "acme")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self._count(calls, "env DEST="), 1,
                         "a connector run still pushed every server")
        self.assertEqual(
            self._count(calls, "DEST=/sandbox/.openclaw/mcp_server_connectors"), 1,
            "the one server it pushed was not the one carrying connector tools")
        self.assertEqual(self._count(calls, "skill install"), 0,
                         "a connector run reinstalled the skills")

    def test_a_connector_run_applies_only_that_connectors_policy(self):
        self._seed_generated()
        p, calls = self._run("--only", "policies,servers", "--connector", "acme")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        adds = [c for c in calls if "policy-add" in c]
        self.assertEqual(len(adds), 1, f"expected one policy-add, got: {adds}")
        self.assertIn("acme", adds[0])

    def test_a_connector_runs_tools_still_reach_the_sandbox(self):
        """Narrowing must not change WHAT lands. The push is still the whole
        merged connectors tree — stage-validate-swap replaces a directory, not a
        file — so the shipped server has to be in there too or the sandbox ends
        up with tools and no server to load them."""
        self._seed_generated()
        p, calls = self._run("--only", "policies,servers", "--connector", "acme")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        members = self._tar_members(calls, "connectors")
        self.assertIn("./apps/acme/acme_call.mjs", members)
        self.assertIn("./_server.mjs", members)

    def test_a_connector_run_still_registers_every_server(self):
        """§4/5 is delete-then-recreate over every ava-* key, so a narrow run
        that registered a subset would unregister the rest — the same trap the
        persona scope has."""
        self._seed_generated()
        full_p, full_calls = self._run()
        self.assertEqual(full_p.returncode, 0, full_p.stdout + full_p.stderr)
        full_names = self._register_call(full_calls).count("ava-")

        self.log.write_text("", encoding="utf-8")
        p, calls = self._run("--only", "policies,servers", "--connector", "acme")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        register = self._register_call(calls)
        self.assertTrue(register, "a connector run did not register servers at all")
        self.assertFalse(register.rstrip().endswith(" []"))
        self.assertEqual(register.count("ava-"), full_names)

    def test_a_connector_run_does_not_write_the_persona(self):
        """Registration has to run, and it used to write IDENTITY.md on the way
        past — so deploying an app silently applied whatever persona ava.yaml
        held, clearing a pending count the owner never pressed Apply for."""
        self._seed_generated()
        p, calls = self._run("--only", "policies,servers", "--connector", "acme")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        register = self._register_call(calls)
        self.assertTrue(register)
        self.assertIn("persona untouched", p.stdout,
                      "a connector run rendered and shipped the persona anyway")
        # The empty positional is the persona; it must be EMPTY, not absent —
        # an absent one would shift every later positional in the exec.
        self.assertIn("node -e", register)

    def test_a_servers_only_run_leaves_the_persona_alone_too(self):
        """Same fix, wider blast radius: `--only servers` had it as well."""
        p, calls = self._run("--only", "servers")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("persona untouched", p.stdout)
        self.assertEqual(self._register_call(calls).count("ava-"),
                         self._register_call(self._run()[1]).count("ava-"))

    def test_a_connector_with_no_generated_policy_is_a_failed_deploy(self):
        """The policy is half of what a connector deploy delivers: without it the
        sandbox denies every route its tools call, so the app is installed and
        unusable. Exiting 0 let the Hub report success — and the post-apply
        verify cannot catch it either, because a policy file that does not exist
        is not in `desired()` and so has no row to check."""
        self._seed_generated()
        p, calls = self._run("--only", "policies,servers", "--connector", "ghost")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(self._count(calls, "policy-add"), 0)
        self.assertIn("no egress policy for 'ghost'", p.stdout + p.stderr)
        self.assertIn("ava connector policies ghost --write", p.stdout + p.stderr)

    def test_a_connector_with_no_only_deploys_that_app_not_everything(self):
        """`--connector X` alone used to default the scope to `all`, so the run
        skipped six of seven policies and four of five servers while printing
        `provisioned` — less than asked, silently."""
        self._seed_generated()
        p, calls = self._run("--connector", "acme")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self._count(calls, "env DEST="), 1)
        self.assertEqual(self._count(calls, "skill install"), 0)
        self.assertEqual(len([c for c in calls if "policy-add" in c]), 1)

    def test_all_plus_connector_is_refused_rather_than_resolved(self):
        p, calls = self._run("--only", "all", "--connector", "acme")
        self.assertEqual(p.returncode, 2)
        self.assertIn("cannot narrow", p.stdout + p.stderr)
        self.assertEqual(calls, [])

    def test_a_connector_id_that_is_a_path_is_refused(self):
        p, calls = self._run("--only", "policies", "--connector", "../../etc")
        self.assertEqual(p.returncode, 2)
        self.assertIn("is not a connector id", p.stdout + p.stderr)
        self.assertEqual(calls, [], "it reached the CLI before validating the id")

    def test_a_connector_with_nothing_to_narrow_is_refused(self):
        p, calls = self._run("--only", "persona", "--connector", "acme")
        self.assertEqual(p.returncode, 2)
        self.assertIn("needs a scope that includes policies or servers",
                      p.stdout + p.stderr)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
