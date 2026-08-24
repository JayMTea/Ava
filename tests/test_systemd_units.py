"""Boot-survival units: what gets captured, and what is refused.

The unit this module writes starts the sandbox's policy plane. Getting it
subtly wrong is worse than not writing it: a gateway pointed at a different
docker daemon, or started with the wrong argv0, LOOKS fine and then fails in a
way that presents as "the agent is broken".

So the tests here are mostly about refusal and about what must survive capture.

House style: stdlib unittest, no bridge, no sandbox, no network.
"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-units-test-"))

from ava_bridge import systemd_units as units


class Argv0Tests(unittest.TestCase):
    """argv0 is how NemoClaw recognises its own gateway. If this drifts, a
    running gateway becomes invisible to the tool that manages it."""

    def test_the_shapes_nemoclaw_accepts(self):
        for good in ("openshell-gateway[nemoclaw=nemoclaw;port=8080]",
                     "openshell-gateway[nemoclaw=nemoclaw-2;port=18789]"):
            self.assertTrue(units.ARGV0_RE.match(good), good)

    def test_the_shapes_it_does_not(self):
        for bad in ("openshell-gateway",
                    "openshell-gateway[nemoclaw=other;port=8080]",
                    "openshell-gateway[nemoclaw=nemoclaw;port=]",
                    "/usr/bin/openshell-gateway[nemoclaw=nemoclaw;port=8080]"):
            self.assertIsNone(units.ARGV0_RE.match(bad), bad)


class EnvCaptureTests(unittest.TestCase):

    def test_docker_host_is_carried(self):
        """It was very nearly dropped. The gateway talks to the daemon over
        DOCKER_HOST, and a unit without it can start a gateway pointed at a
        DIFFERENT daemon than the one that was working — while looking right."""
        self.assertIn("DOCKER_HOST", units.ENV_NAMES)

    def test_the_capture_is_an_allow_list(self):
        """A process environment holds the operator's tokens, and a unit file is
        world-readable. Carrying everything and removing known-bad names is the
        shape that leaks the next variable somebody adds."""
        src = open(units.__file__, encoding="utf-8").read()
        self.assertIn("ENV_PREFIXES", src)
        self.assertIn("ENV_NAMES", src)
        self.assertNotIn("ENV_DENY", src)

    def test_nothing_credential_shaped_is_allowed(self):
        for name in units.ENV_NAMES:
            for bad in ("TOKEN", "SECRET", "PASSWORD", "KEY="):
                self.assertNotIn(bad, name.upper())


class PortabilityTests(unittest.TestCase):

    def test_home_becomes_the_systemd_specifier(self):
        """A unit carrying one operator's absolute home is not installable by
        anyone else, which is the whole reason these lived on one machine."""
        home = os.path.expanduser("~")
        got = units._portable(f"sqlite:{home}/.local/state/x/y.db")
        self.assertEqual(got, "sqlite:%h/.local/state/x/y.db")
        self.assertNotIn(home, got)


class RefusalTests(unittest.TestCase):

    def test_a_missing_gateway_is_refused_rather_than_guessed(self):
        """The environment is chosen by `nemoclaw onboard` and includes an image
        pinned by DIGEST. There is no honest default for that, so not-running
        must be an error and never a rendered file."""
        real = units.gateway_process
        units.gateway_process = lambda: None
        try:
            with self.assertRaises(units.CaptureError) as cm:
                units.render_gateway_unit()
            self.assertIn("not running", str(cm.exception))
        finally:
            units.gateway_process = real

    def test_a_gateway_with_no_openshell_env_is_refused(self):
        """Started some other way than onboard — capturing it would write a unit
        that starts it differently again."""
        real = units.gateway_process
        units.gateway_process = lambda: {
            "pid": 1, "argv0": "openshell-gateway[nemoclaw=nemoclaw;port=1]",
            "exe": "/x", "env": {}}
        try:
            with self.assertRaises(units.CaptureError):
                units.render_gateway_unit()
        finally:
            units.gateway_process = real


class ManagedTagTests(unittest.TestCase):

    def test_removal_only_ever_touches_what_ava_wrote(self):
        """`--remove` deleting a hand-written unit would be destroying someone
        else's work, so the tag is the gate and it must be checked by CONTENT."""
        d = tempfile.mkdtemp()
        real_dir = units.UNIT_DIR
        units.UNIT_DIR = d
        try:
            open(os.path.join(d, "openshell-gateway.service"), "w").write(
                "[Unit]\nDescription=hand written\n")
            self.assertFalse(units.is_managed("openshell-gateway.service"))
            open(os.path.join(d, "openshell-gateway.service"), "w").write(
                units.MANAGED_TAG + "\n[Unit]\n")
            self.assertTrue(units.is_managed("openshell-gateway.service"))
        finally:
            units.UNIT_DIR = real_dir

    def test_an_absent_unit_is_not_managed(self):
        d = tempfile.mkdtemp()
        real_dir = units.UNIT_DIR
        units.UNIT_DIR = d
        try:
            self.assertFalse(units.is_managed("openshell-gateway.service"))
        finally:
            units.UNIT_DIR = real_dir


class TemplateTests(unittest.TestCase):

    def test_the_template_ships_in_the_repo(self):
        """The point of the whole slice: a fork gets the kit."""
        path = os.path.join(units.TEMPLATES, "openshell-gateway.service.tmpl")
        self.assertTrue(os.path.exists(path),
                        "the unit template must be tracked, or these units "
                        "again exist only on one machine")

    def test_the_template_keeps_the_argv0_exec_trick(self):
        """systemd has no argv0 option, so `exec -a` is doing real work. A
        refactor that 'simplifies' it to a plain ExecStart makes the gateway
        unrecognisable to nemoclaw."""
        with open(os.path.join(units.TEMPLATES,
                               "openshell-gateway.service.tmpl"),
                  encoding="utf-8") as f:
            body = f.read()
        self.assertIn("exec -a", body)
        self.assertIn("{ARGV0}", body)
        self.assertIn("{ENVIRONMENT}", body)

    def test_the_template_waits_for_docker(self):
        """`After=docker.service` is inert in a USER unit — docker.service is a
        system unit. Without the explicit wait the gateway burns its restarts
        before the daemon is up."""
        with open(os.path.join(units.TEMPLATES,
                               "openshell-gateway.service.tmpl"),
                  encoding="utf-8") as f:
            body = f.read()
        self.assertIn("ExecStartPre", body)
        self.assertIn("docker info", body)


if __name__ == "__main__":
    unittest.main()
