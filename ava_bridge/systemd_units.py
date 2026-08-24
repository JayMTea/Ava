"""Boot-survival units, rendered from what is ACTUALLY running.

WHY THIS EXISTS
---------------
The OpenShell host gateway is spawned detached by `nemoclaw onboard` and has no
supervisor. On reboot it is simply gone, and the sandbox then crash-loops
fetching a policy nobody is serving — which presents as "the agent is broken"
rather than "its gateway is missing". The fix is a user unit, and until now that
unit existed only on one machine: a fork got none of it, and the owner's own
setup was not reproducible from the repo.

WHY IT CAPTURES RATHER THAN TEMPLATES
-------------------------------------
The gateway's environment is a dozen values that `nemoclaw onboard` chose —
database URL, docker network, TLS directory, and a SUPERVISOR IMAGE PINNED BY
DIGEST. None of that can be derived from Ava's own settings, and inventing
plausible values is how you get a unit that starts a subtly different gateway
than the one that was working. So the environment is read from the running
process (`/proc/<pid>/environ`), which is the only authoritative source, and the
unit is refused outright when the gateway is not running. "Start it, then
capture it" is honest; "here is a guess" is not.

Home is rewritten to systemd's `%h` so the rendered unit is portable between
users rather than carrying one operator's path.
"""
from __future__ import annotations

import os
import re
import subprocess

from . import settings

# NemoClaw identifies its own gateway by this exact argv0 shape; see the note in
# the template. Captured, not guessed:
# src/lib/onboard/gateway-process-target-identity.ts
ARGV0_RE = re.compile(r"^openshell-gateway\[nemoclaw=(nemoclaw(?:-\d+)?);port=(\d+)\]$")

# Every unit this module writes carries this line, so `--remove` can tell what
# Ava installed from what the operator wrote by hand. Removing a unit we did not
# write would be destroying someone else's work.
MANAGED_TAG = "# Managed by Ava — `ava agent install-units`. Safe to delete."

UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
TEMPLATES = os.path.join(str(settings.CODE_ROOT), "deploy", "systemd")

#: Only these variables are carried into the unit. A process environment also
#: holds the operator's shell, tokens and PATH, and a unit file is
#: world-readable — so this is an ALLOW list, never a deny list.
#:
#: DOCKER_* is here because it is load-bearing and was very nearly missed: the
#: gateway talks to the daemon over DOCKER_HOST, and this box sets it to
#: `unix:///run/docker.sock` while docker's own default is
#: `unix:///var/run/docker.sock`. Those happen to resolve to the same socket
#: here, and would not everywhere. Dropping it produced a unit that looked right
#: and could start a gateway pointed at a different daemon. None of these carry
#: a credential; DOCKER_CERT_PATH is a directory, not a key.
ENV_PREFIXES = ("OPENSHELL_",)
ENV_NAMES = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CERT_PATH",
             "DOCKER_TLS_VERIFY")


class CaptureError(RuntimeError):
    """The live facts a unit needs are not available."""


def _home() -> str:
    return os.path.expanduser("~")


def _portable(value: str) -> str:
    """Rewrite this operator's home to systemd's `%h`."""
    home = _home()
    return value.replace(home, "%h") if home and home != "/" else value


def gateway_process() -> dict | None:
    """The running OpenShell gateway: pid, argv0, exe and its environment.

    Returns None when it is not running. Reads /proc directly rather than
    shelling out to pgrep: the argv0 IS the identity here, and `ps` truncation
    or a shell's own matching process are exactly the kind of noise that makes a
    captured value wrong.
    """
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                argv0 = f.read().split(b"\0")[0].decode("utf-8", "replace")
        except OSError:
            continue
        if not ARGV0_RE.match(argv0):
            continue
        try:
            exe = os.readlink(f"/proc/{entry}/exe")
            with open(f"/proc/{entry}/environ", "rb") as f:
                raw = f.read()
        except OSError as e:
            raise CaptureError(
                f"found the gateway (pid {entry}) but could not read it: {e}. "
                "It is running as another user, so its environment cannot be "
                "captured from here.") from e
        env = {}
        for item in raw.split(b"\0"):
            if not item:
                continue
            k, _, v = item.decode("utf-8", "replace").partition("=")
            if k.startswith(ENV_PREFIXES) or k in ENV_NAMES:
                env[k] = v
        return {"pid": int(entry), "argv0": argv0, "exe": exe, "env": env}
    return None


def render_gateway_unit() -> str:
    """The unit text for the gateway that is running right now."""
    proc = gateway_process()
    if proc is None:
        raise CaptureError(
            "the OpenShell gateway is not running, so there is nothing to "
            "capture. Its environment (database URL, docker network, TLS "
            "directory, and a supervisor image pinned by digest) is chosen by "
            "`nemoclaw onboard` and cannot be derived — start the gateway, "
            "then run this again.")
    if not proc["env"]:
        raise CaptureError(
            f"the gateway (pid {proc['pid']}) has no OPENSHELL_* variables in "
            "its environment, which means it was started some other way than "
            "`nemoclaw onboard`. Refusing to write a unit that would start it "
            "differently.")
    with open(os.path.join(TEMPLATES, "openshell-gateway.service.tmpl"),
              encoding="utf-8") as f:
        tmpl = f.read()
    lines = [f"Environment={k}={_portable(v)}"
             for k, v in sorted(proc["env"].items())]
    body = (tmpl
            .replace("{ENVIRONMENT}", "\n".join(lines))
            .replace("{ARGV0}", proc["argv0"])
            .replace("{EXE}", _portable(proc["exe"])))
    return MANAGED_TAG + "\n" + body


UNITS = {"openshell-gateway.service": render_gateway_unit}


def installed(name: str) -> str | None:
    """What is on disk at the unit's path, or None."""
    path = os.path.join(UNIT_DIR, name)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def is_managed(name: str) -> bool:
    """Did Ava write the installed copy? Only those may be removed."""
    body = installed(name)
    return bool(body and MANAGED_TAG in body)


def plan(name: str) -> dict:
    """What installing this unit would do, without doing any of it."""
    try:
        want = UNITS[name]()
    except CaptureError as e:
        return {"unit": name, "action": "blocked", "reason": str(e)}
    have = installed(name)
    if have is None:
        return {"unit": name, "action": "create", "text": want}
    if have == want:
        return {"unit": name, "action": "unchanged", "text": want}
    return {"unit": name, "action": "update", "text": want, "current": have,
            "managed": MANAGED_TAG in have}


def write(name: str, text: str) -> str:
    os.makedirs(UNIT_DIR, exist_ok=True)
    path = os.path.join(UNIT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def daemon_reload() -> tuple[bool, str]:
    """Ask systemd to re-read units. Never fatal: the file is already written,
    and an operator without a user bus can reload it themselves."""
    try:
        r = subprocess.run(["systemctl", "--user", "daemon-reload"],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0, (r.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
