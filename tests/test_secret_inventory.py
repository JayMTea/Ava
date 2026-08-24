"""Every secret the code writes must appear in SECURITY.md's inventory.

§4 is what an operator reads before backing up, rotating, or handing a machine
on. A credential the code writes but the inventory omits is one nobody knows to
protect — and the inventory drifted twice without anyone noticing:

  * `secrets/openclaw_client_id` was written on every gateway handshake and
    documented nowhere.
  * `ava setup` generated `secrets/session_secret`, a 0600 credential-shaped
    file that NOTHING reads — the key that actually signs cookies is
    `data/.secret`. Setup reported "session secret: generated" about a file
    with no bearing on anything, so rotating it changed nothing and said it had.

Both are the same failure: the inventory and the code had no way to disagree
out loud. This is that way.

House style is tests/test_diagram_sync.py — a static scan, no bridge, no
network, failing with instructions.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECURITY = os.path.join(ROOT, "SECURITY.md")

#: `settings.secret("<name>", ...)` — the one helper that resolves-or-generates
#: a file under `secrets/`. Anything it names is a file on an operator's disk.
SECRET_CALL = re.compile(r"""settings\.secret\(\s*["']([a-z0-9_]+)["']""")

#: Files under `secrets/` built as a path rather than named through the helper.
#: Deliberately requires an explicit join or `Path(...) /`: a looser
#: "secrets_dir(), <string>" pattern also matches ordinary calls that merely
#: PASS the directory along, and reported `_store(..., secrets_dir(), "locked")`
#: as a secret named "locked".
SECRET_PATH = re.compile(
    r"""(?:os\.path\.join\(\s*settings\.secrets_dir\(\)\s*,"""
    r"""|Path\(\s*settings\.secrets_dir\(\)\s*\)\s*/)\s*["']([a-z0-9_]+)["']""")

SOURCES = ("ava_bridge", "ava_cli.py", "phone_bridge.py")

#: Names that are a TEMPLATE rather than a file (documented as a pattern).
TEMPLATED = {"env"}


def _walk():
    for entry in SOURCES:
        path = os.path.join(ROOT, entry)
        if os.path.isfile(path):
            yield entry, open(path, encoding="utf-8").read()
            continue
        for base, _dirs, files in os.walk(path):
            for fn in files:
                if fn.endswith(".py"):
                    full = os.path.join(base, fn)
                    yield (os.path.relpath(full, ROOT),
                           open(full, encoding="utf-8").read())


def _written() -> dict[str, str]:
    """{secret name: the file that writes it}."""
    found: dict[str, str] = {}
    for rel, body in _walk():
        for rx in (SECRET_CALL, SECRET_PATH):
            for m in rx.finditer(body):
                found.setdefault(m.group(1), rel)
    return found


class InventoryTests(unittest.TestCase):

    def test_the_scan_finds_the_secrets_we_know_exist(self):
        """Anti-vacuous: a regex that has drifted from the call sites would
        make every assertion below pass by finding nothing."""
        got = _written()
        self.assertIn("router_token", got)
        self.assertIn("openclaw_gateway_token", got)
        self.assertGreaterEqual(len(got), 4)

    def test_every_written_secret_is_in_the_inventory(self):
        doc = open(SECURITY, encoding="utf-8").read()
        missing = []
        for name, rel in sorted(_written().items()):
            if name in TEMPLATED:
                continue
            if f"secrets/{name}" not in doc:
                missing.append(f"{name} (written by {rel})")
        self.assertEqual(
            missing, [],
            "these are written to $AVA_HOME/secrets/ but are absent from "
            "SECURITY.md §4, so an operator backing up or rotating has no way "
            "to know they exist:\n  " + "\n  ".join(missing))

    def test_no_secret_is_generated_that_nothing_reads(self):
        """A credential-shaped file nobody consumes is worse than none: it
        invites rotation that accomplishes nothing, and `ava setup` reported
        exactly that for `session_secret` — which no code has ever read."""
        written = _written()
        orphans = []
        for name in written:
            if name in TEMPLATED:
                continue
            readers = 0
            for rel, body in _walk():
                # A write is `settings.secret("x", ..., generate=True)`; any
                # OTHER mention of the name is a read (or a second write).
                hits = len(re.findall(rf"""["']{re.escape(name)}["']""", body))
                writes = len([m for m in SECRET_CALL.finditer(body)
                              if m.group(1) == name])
                readers += hits - writes
            if readers == 0:
                orphans.append(name)
        self.assertEqual(
            orphans, [],
            "generated but never read anywhere — either wire it up or stop "
            "writing it; a dead credential file invites a rotation that "
            f"silently does nothing: {orphans}")


if __name__ == "__main__":
    unittest.main()
