"""The `ava` CLI as a user drives it, each command in a subprocess against an
isolated AVA_HOME: setup, doctor, connector generators, device tokens.
"""
import os
import subprocess
import sys
import tempfile
import unittest

from qa.env_recipe import hermetic_env, shims_pythonpath

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(_REPO, "ava_cli.py")

HOME = tempfile.mkdtemp(prefix="ava-qa-cli-")


def _run(*args: str, timeout: int = 120):
    env = dict(os.environ)
    env.update(hermetic_env(HOME, "http://127.0.0.1:1/v1/chat/completions"))
    env["PYTHONPATH"] = shims_pythonpath() + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, CLI, *args], cwd=_REPO, env=env,
                          capture_output=True, text=True, timeout=timeout)


class TestCliSetupAndDoctor(unittest.TestCase):
    def test_01_setup_provisions_the_home(self):
        r = _run("setup", "--password", "qa-cli-password-1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for rel in ("data", "logs", "secrets", "ava.yaml"):
            self.assertTrue(os.path.exists(os.path.join(HOME, rel)),
                            f"{rel} missing after `ava setup`")
        pw = open(os.path.join(HOME, "data", "auth_password"),
                  encoding="utf-8").read().strip()
        self.assertEqual(pw, "qa-cli-password-1")

    def test_02_doctor_runs_clean_enough(self):
        r = _run("doctor")
        out = r.stdout + r.stderr
        # Doctor may warn about absent services, but it must complete and print
        # its report — a crash is a real failure. Exit 2 is a REPORTED verdict,
        # not a crash: "the report ran, and no inference backend is reachable".
        # The QA rig serves no engine, so 2 is the expected code here — and it is
        # the whole point, since `ava setup && ava doctor && ava up` must stop
        # rather than hand the user a chat box that 503s on the first message.
        self.assertIn(r.returncode, (0, 2), out)
        self.assertIn("config", out.lower())
        self.assertNotIn("Traceback", out)
        if r.returncode == 2:
            self.assertIn("No inference backend is reachable", out)

    def test_03_version(self):
        r = _run("version")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.strip())


class TestCliConnectors(unittest.TestCase):
    def test_connector_list_and_generators_lockstep(self):
        r = _run("connector", "list")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("bridge", r.stdout)
        # Rendering tools/policies for a built-in must be deterministic and
        # exit clean (the deep determinism check lives in tests/).
        r = _run("connector", "policies", "bridge")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_device_scaffold_and_token(self):
        r = _run("device", "new", "qaclidev")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(
            HOME, "connectors", "qaclidev", "connector.yaml")))
        r = _run("device", "token", "qaclidev")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        token = r.stdout.strip().splitlines()[-1].strip()
        self.assertGreaterEqual(len(token), 32, f"no token printed: {r.stdout!r}")
