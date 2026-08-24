"""The bridge and `agent/install.sh` must agree about WHICH sandbox they mean.

They resolve the same three facts from two different places. The bridge reads
them through `settings`, so `ava.yaml` is first-class:

    agent.sandbox      -> config.OC_SANDBOX          (env AVA_OC_SANDBOX)
    agent CLI          -> config.OC_NEMOCLAW         (env AVA_NEMOCLAW, else PATH)
    paths.data         -> settings.data_dir()        (env AVA_DATA_DIR)
    paths.agent_state  -> settings.agent_state_dir() (env AVA_AGENT_STATE_DIR)

`install.sh` is a shell script with no YAML parser. It reads the environment and
nothing else — `${AVA_OC_SANDBOX:-my-assistant}`, `${AVA_NEMOCLAW:-$HOME/…}`,
`${AVA_DATA_DIR:-…}`. So every caller that shells it with a bare `os.environ`
hands it a DIFFERENT answer than the one the bridge will use to check the work:

  * A `agent.sandbox:` in ava.yaml deployed into `my-assistant` while drift
    inspected the configured name. Every scope read `undeployed` forever, every
    Apply reported success, and nothing named the cause.
  * A `nemoclaw` binary anywhere but `$HOME/.local/bin` (systemd user services
    get a different HOME) was simply not found, in a script that had already
    passed the bridge's own "the CLI resolves" check.
  * A `paths.data:` in ava.yaml put the internal token somewhere the bridge does
    not read — the documented 401-on-every-`/internal/*`-callback incident, one
    layer up from where it was fixed.

Docker never hit any of it, because `deploy/agent-entrypoint.sh` exports the
values by hand. That is exactly why bare metal and WSL2 are the exposed paths.

`NemoClawRuntime.install_env()` is the one place that carries the bridge's answer
across; these tests pin that every caller uses it and that it stays complete.

House style matches tests/test_install_scope_contract.py and
tests/test_path_roots.py: a static scan plus a pure unit check — no bridge, no
AVA_HOME, no sandbox, no network.
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "agent" / "install.sh"

# Every `NAME="${AVA_...:-default}"` assignment in install.sh: the facts it takes
# from the environment because it cannot read ava.yaml.
_ENV_DEFAULTED = re.compile(r'^\s*[A-Z_]+="\$\{(AVA_[A-Z_]+)')

# Of those, the ones the bridge ALSO resolves through settings/config — so
# ava.yaml can move them and the shell would never know. These are the ones
# install_env() has to pin. A new entry in install.sh is not automatically a
# problem; it is a decision, which is why the test names it rather than guessing.
MUST_PIN = {"AVA_OC_SANDBOX", "AVA_NEMOCLAW", "AVA_DATA_DIR",
            "AVA_SECRETS_DIR", "AVA_AGENT_STATE_DIR"}

# Read from the environment on BOTH sides with equivalent defaults, so no split
# is possible: install.sh inherits whatever the bridge process was given.
ENV_ONLY_BOTH_SIDES = {"AVA_OVERLAY", "AVA_HOME"}


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", pattern],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


class InstallShContractTests(unittest.TestCase):

    def test_every_env_fact_install_sh_reads_is_accounted_for(self):
        """A new `${AVA_*:-…}` in install.sh is a new chance to disagree."""
        found = {m.group(1)
                 for line in INSTALL_SH.read_text(encoding="utf-8").splitlines()
                 if (m := _ENV_DEFAULTED.search(line))}
        unaccounted = found - MUST_PIN - ENV_ONLY_BOTH_SIDES
        self.assertFalse(
            unaccounted,
            f"agent/install.sh now resolves {sorted(unaccounted)} from the "
            "environment. Decide which side owns it: if the bridge resolves the "
            "same fact through settings/config (so ava.yaml can move it), add it "
            "to NemoClawRuntime.install_env() and to MUST_PIN here. If both sides "
            "read the same env var with the same default, add it to "
            "ENV_ONLY_BOTH_SIDES.")

    def test_install_sh_still_reads_the_three_we_pin(self):
        """The mirror of the above: if install.sh stops reading one of these,
        install_env() is pinning something nothing consumes."""
        text = INSTALL_SH.read_text(encoding="utf-8")
        for name in sorted(MUST_PIN):
            self.assertIn(
                name, text,
                f"install_env() pins {name} but agent/install.sh no longer reads "
                "it — drop it from both, or the pin is cargo cult.")


class InstallEnvTests(unittest.TestCase):

    def test_it_carries_the_bridges_answer_not_the_processs(self):
        """The whole point: values come from the resolver, NOT from os.environ.

        Set via ava.yaml (which is what `config.OC_SANDBOX` models here), the
        variables are absent from the environment entirely — and that is the
        case that shipped broken.
        """
        from ava_bridge import settings
        from ava_bridge.runtime.nemoclaw import NemoClawRuntime

        rt = NemoClawRuntime()
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("ava_bridge.config.OC_SANDBOX", "my-ava"), \
             mock.patch("ava_bridge.config.OC_NEMOCLAW", "/opt/bin/nemoclaw"), \
             mock.patch.object(settings, "data_dir", return_value="/mnt/ava/data"):
            env = rt.install_env()

        self.assertEqual(env["AVA_OC_SANDBOX"], "my-ava")
        self.assertEqual(env["AVA_NEMOCLAW"], "/opt/bin/nemoclaw")
        self.assertEqual(env["AVA_DATA_DIR"], "/mnt/ava/data")

    def test_it_pins_every_variable_it_promises_to(self):
        from ava_bridge.runtime.nemoclaw import NemoClawRuntime

        env = NemoClawRuntime().install_env()
        self.assertEqual(MUST_PIN - set(env), set(),
                         "install_env() is missing a variable install.sh reads")

    def test_it_extends_the_environment_rather_than_replacing_it(self):
        """install.sh needs PATH, HOME and the rest to run at all — a replaced
        environment breaks it in a way that looks like a sandbox failure."""
        from ava_bridge.runtime.nemoclaw import NemoClawRuntime

        with mock.patch.dict("os.environ", {"PATH": "/usr/bin", "UNRELATED": "1"}):
            env = NemoClawRuntime().install_env()
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["UNRELATED"], "1")


class CallSiteTests(unittest.TestCase):

    def test_every_caller_of_install_sh_passes_an_env(self):
        """Static scan. Two callers exist today — `NemoClawRuntime.provision`
        and the connector deploy route — and both regressed the same way by
        simply not thinking about the environment.
        """
        offenders: list[str] = []
        for rel in _tracked("*.py"):
            if rel.startswith("tests/"):
                continue
            text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
            if "install.sh" not in text:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                # The shell-out itself, not the path construction or a docstring.
                if not re.search(r'(subprocess\.(run|Popen)|_run|_stream)\(', line):
                    continue
                window = "\n".join(text.splitlines()[i - 1:i + 8])
                if "install" not in window or "bash" not in window:
                    continue
                if "env=" not in window:
                    offenders.append(f"{rel}:{i}")

        self.assertFalse(
            offenders,
            "these shell agent/install.sh without an environment, so it will "
            "resolve the sandbox name, CLI path and data dir from the process "
            f"environment instead of from ava.yaml: {offenders}. "
            "Pass env=<runtime>.install_env().")


if __name__ == "__main__":
    unittest.main()


class SandboxCheckTests(unittest.TestCase):
    """The bootstrap guard must ask a real question.

    `grep -q "\"$SANDBOX\""` over `nemoclaw list --json` matches the name
    ANYWHERE in the output — including inside `{"error":"sandbox 'ava' not
    found"}`, and inside a different sandbox's name (`ava` matches `ava-old`).
    `ava_bridge/runtime/nemoclaw.py` removed exactly this and documents why; the
    shell copy kept it, so the two halves of one question could disagree.
    """

    def test_the_shell_parses_the_json_instead_of_grepping_it(self):
        # Code only. The comment above the replacement quotes the old pattern on
        # purpose, and a guard that forbids explaining a bug is a bad guard.
        code = "\n".join(ln for ln in INSTALL_SH.read_text(encoding="utf-8").splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertNotIn("grep -q", code, "the substring check is back")
        self.assertIn("_sandbox_exists", code)
        self.assertIn("json.load", code,
                      "the existence check no longer parses the CLI's JSON")

    def test_it_does_not_recommend_the_stub_npm_package(self):
        """`npm install -g nemoclaw` is an empty stub — the docs and the runtime
        adapter both say so, while this script told people to install it."""
        sh = INSTALL_SH.read_text(encoding="utf-8")
        recommended = [ln for ln in sh.splitlines()
                       if "npm install -g nemoclaw" in ln
                       and not ln.lstrip().startswith("#")]
        self.assertFalse(recommended, recommended)


class InstallRefTests(unittest.TestCase):
    """`ava agent provision --install` must install the ref Docker pins.

    `deploy/agent.Dockerfile` pins `NEMOCLAW_INSTALL_REF` so bare-metal and
    Docker installs get the SAME agent runtime, and docs/AGENT_RUNTIME.md calls
    that pin load-bearing — it even tells a human to read the ref out of the
    Dockerfile rather than hardcode it. The adapter then installed from `main`
    regardless, so the one command the docs recommend produced exactly the drift
    the ARG exists to prevent.
    """

    def test_it_reads_the_ref_the_container_path_pins(self):
        from ava_bridge.runtime.nemoclaw import _install_ref

        dockerfile = (ROOT / "deploy" / "agent.Dockerfile").read_text(encoding="utf-8")
        pinned = next(ln.split("=", 1)[1].strip()
                      for ln in dockerfile.splitlines()
                      if ln.startswith("ARG NEMOCLAW_INSTALL_REF="))
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_install_ref(), pinned)

    def test_the_env_var_is_still_the_escape_hatch(self):
        """The Dockerfile documents `NEMOCLAW_INSTALL_REF=main` as the way out
        when a pin goes bad; that has to keep working."""
        from ava_bridge.runtime.nemoclaw import _install_ref

        with mock.patch.dict("os.environ", {"NEMOCLAW_INSTALL_REF": "main"}):
            self.assertEqual(_install_ref(), "main")

    def test_the_installer_url_carries_it(self):
        import inspect

        from ava_bridge.runtime.nemoclaw import NemoClawRuntime

        src = inspect.getsource(NemoClawRuntime.provision)
        self.assertIn("_install_ref()", src)
        self.assertNotIn("NemoClaw/main/install.sh", src,
                         "the installer is hardcoded to main again")
