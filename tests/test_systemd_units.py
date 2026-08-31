"""Boot-survival units: what gets captured, and what is refused.

The unit this module writes starts the sandbox's policy plane. Getting it
subtly wrong is worse than not writing it: a gateway pointed at a different
docker daemon, or started with the wrong argv0, LOOKS fine and then fails in a
way that presents as "the agent is broken".

So the tests here are mostly about refusal and about what must survive capture.

House style: stdlib unittest, no bridge, no sandbox, no network.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
from unittest import mock

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


# --------------------------------------------------------------------------- #
# The dashboard port-forward.
#
# OpenClaw's gateway is reachable only through a forward into the sandbox, and
# `nemoclaw recover` / `connect` spawn it DETACHED — reparented to init, with
# nothing supervising it. Observed for real: an orphaned ssh (PPID 1) held the
# port for two days while `nemoclaw-recover.service`, a Type=oneshot BOOT
# restorer, sat in `failed (Result: timeout)`. A boot-time restorer is not a
# supervisor. These tests pin the two properties that make this unit one.
# --------------------------------------------------------------------------- #
FORWARD = "nemoclaw-dashboard-forward.service"

_RECORD = {"name": "my-assistant", "gatewayName": "nemoclaw",
           "dashboardPort": 18789, "gatewayPort": 8080}


def _forward_template() -> str:
    with open(os.path.join(units.TEMPLATES, FORWARD + ".tmpl"),
              encoding="utf-8") as f:
        return f.read()


def _forward_directives() -> str:
    """The template with its comments stripped.

    The comments here quote the very strings the tests forbid — they explain
    that `nemoclaw-recover.service` is `Type=oneshot` and that `-d` is the bug.
    Asserting against raw text made the file fail for describing the problem it
    fixes, which would teach the next author to write a thinner comment.
    """
    return "\n".join(ln for ln in _forward_template().splitlines()
                     if not ln.lstrip().startswith("#"))


class ForwardTemplateTests(unittest.TestCase):

    def test_the_template_ships_in_the_repo(self):
        self.assertTrue(os.path.exists(os.path.join(units.TEMPLATES,
                                                    FORWARD + ".tmpl")))

    def test_it_is_registered_so_install_units_writes_it(self):
        """A template nobody renders is a file, not a fix."""
        self.assertIn(FORWARD, units.UNITS)

    def test_the_forward_runs_in_the_FOREGROUND(self):
        """THE REGRESSION. `openshell forward start -d` returns immediately and
        leaves an unsupervised child — which is the bug, not the fix. In the
        foreground the forward IS the main process, so systemd sees it die."""
        exec_start = [ln for ln in _forward_directives().splitlines()
                      if ln.startswith("ExecStart=")]
        self.assertEqual(len(exec_start), 1, "expected exactly one ExecStart")
        line = exec_start[0]
        self.assertIn("forward start", line)
        self.assertNotIn(" -d", line,
                         "`-d` detaches the forward and re-creates the orphan "
                         "this unit exists to eliminate")
        self.assertNotIn("--background", line)

    def test_it_is_supervised_rather_than_a_oneshot(self):
        """`nemoclaw-recover.service` is Type=oneshot and that is precisely why
        the forward it restores becomes an orphan."""
        body = _forward_directives()
        self.assertIn("Type=simple", body)
        self.assertIn("Restart=always", body)
        self.assertNotIn("Type=oneshot", body)

    def test_it_clears_a_stale_forward_before_starting(self):
        """`forward start` is not idempotent against a forward OpenShell still
        tracks, so a restart would fail on the bound port forever."""
        body = _forward_directives()
        self.assertIn("forward stop", body)
        self.assertIn("ExecStartPre=-", body,
                      "the stop must be prefixed `-`; a no-op stop is the "
                      "normal case and must not fail the unit")

    def test_it_waits_for_the_host_gateway(self):
        """The forward is minted THROUGH the host gateway. `After=` orders, it
        does not wait for readiness."""
        self.assertIn("/dev/tcp/127.0.0.1/", _forward_directives())

    def test_it_does_not_hard_require_a_unit_that_may_not_exist(self):
        """`Requires=` on an uninstalled unit makes THIS one unloadable, turning
        a missing dependency into a confusing hard failure. The ExecStartPre
        gates by observation instead."""
        self.assertNotIn("Requires=openshell-gateway.service",
                         _forward_directives())


class ForwardRenderTests(unittest.TestCase):

    def _patches(self, record=_RECORD, port=18789, gw_port=8080,
                 sandbox="my-assistant", binary="/home/x/.local/bin/openshell"):
        from ava_bridge import config
        from ava_bridge.runtime import nemoclaw_registry as reg
        return [
            mock.patch.object(config, "OC_SANDBOX", sandbox),
            mock.patch.object(reg, "registry_record", lambda *a, **k: record),
            mock.patch.object(reg, "openclaw_gateway_port", lambda *a, **k: port),
            mock.patch.object(reg, "openshell_gateway_port", lambda *a, **k: gw_port),
            mock.patch.object(units, "openshell_bin", lambda: binary),
        ]

    def _render(self, **kw) -> str:
        with contextlib.ExitStack() as stack:
            for p in self._patches(**kw):
                stack.enter_context(p)
            return units.render_dashboard_forward_unit()

    def test_it_renders_every_placeholder(self):
        body = self._render()
        self.assertNotIn("{", body.replace("${", ""),
                         f"an unreplaced placeholder shipped into the unit:\n{body}")
        self.assertIn("18789", body)
        self.assertIn("my-assistant", body)
        self.assertIn(units.MANAGED_TAG, body)

    def test_the_dashboard_port_is_not_confused_with_the_gateway_port(self):
        """`dashboardPort` (OpenClaw's JSON-RPC + Control UI) and `gatewayPort`
        (OpenShell's mTLS plane) are different services with different trust
        models. Forwarding the wrong one points the control plane at a daemon
        that has no idea what to do with an operator token."""
        body = self._render(port=18789, gw_port=8080)
        self.assertIn("forward start 18789", body)
        self.assertIn("/dev/tcp/127.0.0.1/8080", body)

    def test_the_operators_home_does_not_ship_in_the_unit(self):
        home = os.path.expanduser("~")
        body = self._render(binary=os.path.join(home, ".local/bin/openshell"))
        self.assertNotIn(home, body)
        self.assertIn("%h/.local/bin/openshell", body)

    # -- refusals: every input is checked, none is defaulted ------------------
    def test_no_sandbox_is_refused(self):
        with self.assertRaises(units.CaptureError):
            self._render(sandbox="")

    def test_an_unknown_sandbox_is_refused(self):
        with self.assertRaises(units.CaptureError) as cm:
            self._render(record=None)
        self.assertIn("registry", str(cm.exception))

    def test_a_missing_dashboard_port_is_refused_not_guessed(self):
        """A forward to a guessed port silently shadows the real one."""
        with self.assertRaises(units.CaptureError) as cm:
            self._render(port=None)
        self.assertIn("dashboardPort", str(cm.exception))

    def test_a_missing_gateway_name_is_refused(self):
        with self.assertRaises(units.CaptureError):
            self._render(record={"name": "my-assistant", "dashboardPort": 18789})

    def test_a_missing_cli_is_refused(self):
        """systemd cannot run an ExecStart that is not there, and it fails in a
        way that reads as the forward being broken."""
        with self.assertRaises(units.CaptureError) as cm:
            self._render(binary=None)
        self.assertIn("openshell", str(cm.exception))

    def test_a_silent_registry_may_default_ONLY_the_readiness_probe(self):
        """The gateway port is a probe target, not a destination: getting it
        wrong delays the start, it does not point the forward somewhere wrong.
        That is the one value allowed a fallback, and it must still render."""
        body = self._render(gw_port=None)
        self.assertIn("/dev/tcp/127.0.0.1/8080", body)


if __name__ == "__main__":
    unittest.main()
