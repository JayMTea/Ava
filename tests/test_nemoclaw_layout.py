"""Vendor layout lives in the runtime package, not scattered through Ava.

The sandbox's filesystem layout and OpenShell's docker topology are facts about
NEMOCLAW. They were spelled literally in seven core modules — connectors,
internal, policy_inventory, setup_wizard, provision, gw_forward, hub/agent —
which is what makes "swap the runtime" a change you cannot make without reading
all of Ava, and what makes a vendor rename a seven-file diff instead of a
one-line one.

`ava_bridge/runtime/nemoclaw_layout.py` owns them now. This holds the tree to it.

House style: a static scan over `git ls-files`, failing with instructions.
"""
from __future__ import annotations

import os
import re
import subprocess
import unittest

from ava_bridge.runtime import nemoclaw_layout as layout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Literals that are vendor knowledge wherever they appear in code.
VENDOR = re.compile(r"""["'](host\.openshell\.internal|/sandbox/[^"']*)["']""")

#: Where they are ALLOWED to be spelled, with the reason.
ALLOWED = {
    # The module that owns them.
    os.path.join("ava_bridge", "runtime", "nemoclaw_layout.py"):
        "this is the module that owns the layout",
    # The adapters ARE the vendor.
    os.path.join("ava_bridge", "runtime", "nemoclaw.py"):
        "the NemoClaw adapter is allowed to know NemoClaw",
    os.path.join("ava_bridge", "runtime", "openclaw_gw.py"):
        "the gateway adapter is allowed to know the gateway's own paths",
    # Not vendor coupling: the name appears as ONE MEMBER of a cross-runtime
    # table of container-host names, beside host.docker.internal (Docker
    # Desktop) and host.containers.internal (Podman). Importing one of the
    # three from a NemoClaw module would imply a relationship that is not
    # there — the wizard is asking "what does any container call its host".
    os.path.join("ava_bridge", "setup_wizard.py"):
        "one entry in a cross-runtime table of container-host gateway names, "
        "not knowledge of NemoClaw's layout",
}


def _sources():
    out = subprocess.run(["git", "ls-files", "ava_bridge"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    for rel in out.split():
        if rel.endswith(".py") and rel.replace("/", os.sep) not in ALLOWED:
            yield rel


class ValueTests(unittest.TestCase):
    """The values must be RIGHT — a tidy module with a wrong path is worse than
    a literal in the right place."""

    def test_the_paths_are_what_the_sandbox_actually_uses(self):
        self.assertEqual(layout.PERSONA_PATH,
                         "/sandbox/.openclaw/workspace/IDENTITY.md")
        self.assertEqual(layout.SKILLS_GLOB,
                         "/sandbox/.openclaw/skills/*/SKILL.md")
        self.assertEqual(layout.CONFIG_PATH,
                         "/sandbox/.openclaw/openclaw.json")
        self.assertEqual(layout.mcp_server_dir("web"),
                         "/sandbox/.openclaw/mcp_server_web")

    def test_the_bridge_host_is_the_name_the_sandbox_resolves(self):
        self.assertEqual(layout.BRIDGE_HOST, "host.openshell.internal")
        self.assertEqual(layout.bridge_url(8096),
                         "http://host.openshell.internal:8096")

    def test_the_port_is_passed_in_rather_than_read(self):
        """Freezing Ava's own server.port inside a vendor module is how a
        rendered egress policy ends up allowing a port the rewrite no longer
        uses — and nothing in the resulting error names a port."""
        import inspect
        src = inspect.getsource(layout)
        self.assertNotIn("SERVER_PORT", src)
        self.assertNotIn("config", src.split('"""', 2)[-1])

    def test_provision_still_exports_the_names_the_tree_uses(self):
        """These were public constants; other modules and tests import them."""
        from ava_bridge import provision
        self.assertEqual(provision.PERSONA_PATH, layout.PERSONA_PATH)
        self.assertEqual(provision.SKILLS_GLOB, layout.SKILLS_GLOB)


class ContainmentTests(unittest.TestCase):

    def test_no_core_module_spells_the_vendors_layout(self):
        bad, scanned = [], 0
        for rel in _sources():
            path = os.path.join(ROOT, rel)
            try:
                with open(path, encoding="utf-8") as f:
                    body = f.read()
            except FileNotFoundError:
                self.fail(f"{rel} is tracked by git but missing from disk")
            scanned += 1
            for m in VENDOR.finditer(body):
                line_start = body.rfind("\n", 0, m.start()) + 1
                line = body[line_start:body.find("\n", m.start())]
                if line.lstrip().startswith("#"):
                    continue        # prose explaining the move is not the move
                bad.append(f"{rel}:{body[:m.start()].count(chr(10)) + 1} "
                           f"spells {m.group(1)}")
        self.assertGreater(scanned, 10, "scanned almost nothing")
        self.assertEqual(
            bad, [],
            "vendor layout spelled outside the runtime package. Import it from "
            "`runtime.nemoclaw_layout` so a vendor change is a one-line diff "
            "instead of a seven-file one:\n  " + "\n  ".join(bad))

    def test_every_allowance_states_a_reason(self):
        for path, why in ALLOWED.items():
            self.assertGreater(len(why), 25, f"{path} allowed without a reason")
            self.assertTrue(os.path.exists(os.path.join(ROOT, path)),
                            f"{path} no longer exists — drop the allowance")


if __name__ == "__main__":
    unittest.main()
