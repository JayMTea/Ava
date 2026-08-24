"""A runtime says what it is; panels do not say it for them.

The Agent runtime panel hardcoded "NemoClaw" in its subtitle and in its install
prompt. That is correct for the DEFAULT runtime and wrong for every other one:
a `direct` install was told what NemoClaw would give it, a `remote` install was
told about a CLI on the wrong machine, and a fork running its own runtime had to
edit UI files to stop being told about somebody else's.

Ava is meant to be cloned and adopted, so the vendor's name belongs to the
adapter that IS the vendor — not to shared copy every fork inherits.

House style: stdlib unittest, no bridge, no sandbox, no network.
"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-desc-test-"))

from ava_bridge import runtime
from ava_bridge.runtime.base import AgentRuntime

NAMES = ("direct", "nemoclaw", "openclaw_gw", "remote")


class ContractTests(unittest.TestCase):

    def test_the_base_class_answers_without_naming_a_vendor(self):
        """The default must be safe for a runtime nobody here wrote."""
        self.assertEqual(AgentRuntime.display_name, "Agent runtime")
        self.assertIsNone(AgentRuntime.install_hint(object()))
        for word in ("nemoclaw", "openclaw"):
            self.assertNotIn(word, AgentRuntime.blurb(object()).lower())

    def test_every_runtime_names_itself(self):
        seen = {}
        for n in NAMES:
            rt = getattr(runtime, n)()
            self.assertTrue(rt.display_name, n)
            self.assertNotEqual(rt.display_name, "Agent runtime",
                                f"{n} did not override display_name")
            seen[rt.display_name] = n
        self.assertEqual(len(seen), len(NAMES),
                         f"two runtimes share a display name: {seen}")

    def test_every_runtime_says_what_it_gets_you(self):
        for n in NAMES:
            blurb = getattr(runtime, n)().blurb()
            self.assertGreater(len(blurb), 40, f"{n}'s blurb says nothing")

    def test_a_runtime_never_describes_a_different_one(self):
        """The actual bug: NemoClaw's words shown on a Direct install."""
        for n in NAMES:
            rt = getattr(runtime, n)()
            text = (rt.blurb() + " " + (rt.install_hint() or "")).lower()
            for other in NAMES:
                if other == n or other == "direct":
                    continue
                # `openclaw_gw`'s sandbox genuinely IS NemoClaw's, so it may
                # name that command; nothing else may name another adapter.
                if n == "openclaw_gw" and other == "nemoclaw":
                    continue
                self.assertNotIn(
                    getattr(runtime, other)().display_name.lower(), text,
                    f"{n} describes itself using {other}'s name")


class PanelTests(unittest.TestCase):
    """The panel must READ these, not restate them."""

    PANEL = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "frontend", "src", "components", "hub", "panels",
        "AgentRuntimePanel.tsx")

    def _visible(self) -> str:
        """Panel source with comment lines removed — a comment explaining the
        old copy is not the old copy."""
        out = []
        for line in open(self.PANEL, encoding="utf-8").read().split("\n"):
            st = line.lstrip()
            if st.startswith("//") or st.startswith("*") or st.startswith("/*"):
                continue
            out.append(line)
        return "\n".join(out)

    def test_the_panel_does_not_hardcode_a_vendor_in_visible_copy(self):
        body = self._visible().lower()
        for word in ("nemoclaw", "openclaw"):
            self.assertNotIn(
                word, body,
                f"'{word}' is hardcoded in panel copy. Read it from the "
                "runtime instead (`st.display_name` / `st.blurb` / "
                "`st.install_hint`) so a fork on another runtime is not told "
                "about this one.")

    def test_the_panel_reads_the_runtimes_own_words(self):
        body = self._visible()
        for field in ("blurb", "display_name", "install_hint"):
            self.assertIn(field, body,
                          f"the panel no longer reads st.{field}")


class StatusTests(unittest.TestCase):

    def test_the_status_payload_carries_them(self):
        """The panel can only read what the bridge sends."""
        from ava_bridge.hub import agent as hub_agent
        src = open(hub_agent.__file__, encoding="utf-8").read()
        for field in ("display_name", "blurb", "install_hint"):
            self.assertIn(f'st["{field}"]', src)


if __name__ == "__main__":
    unittest.main()
