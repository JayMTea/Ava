"""The kept-alive Agent view must actually be hidden on other tabs.

App.tsx deliberately never unmounts AgentView once visited — unmounting would
kill the terminal PTY and the browser panel's state. The hiding is therefore a
CONTRACT between two files: AgentView.tsx stamps `data-active` on its root, and
agent.css turns `data-active='0'` into display:none. Either half alone is
silently useless — an attribute nothing styles, or a selector nothing stamps —
and the failure mode is not an error but a double exposure: the console painting
over whatever tab the user is actually on. Observed live: after one visit to
#agent, the session list and "New session" button showed through the Chats tab.

(ViewErrorBoundary's `hidden` prop is not the mechanism: it hides only its own
crash panel, never a healthy child — see ViewErrorBoundary.tsx.)

House style is tests/test_icon_sizing.py — a static scan, no build, no browser.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FE = os.path.join(ROOT, "frontend", "src")


def _read(*parts: str) -> str:
    with open(os.path.join(FE, *parts), encoding="utf-8") as f:
        return f.read()


class KeepAliveHiddenTests(unittest.TestCase):

    def test_the_view_stamps_its_active_state(self):
        tsx = _read("components", "agent", "AgentView.tsx")
        self.assertRegex(
            tsx, r"data-active=\{active",
            "AgentView must stamp data-active from its `active` prop — the "
            "CSS below has nothing to select otherwise")

    def test_the_stylesheet_hides_the_inactive_view(self):
        css = _read("styles", "agent.css")
        m = re.search(r"\.agent\[data-active=.0.\]\s*\{([^}]*)\}", css)
        self.assertIsNotNone(
            m, "agent.css must hide .agent[data-active='0'] — without it the "
               "kept-alive console paints over every other tab")
        self.assertIn("display: none", m.group(1),
                      "the inactive view must leave layout and paint entirely; "
                      "visibility alone still occupies the viewport")

    def test_the_view_is_kept_alive_in_the_shell(self):
        """If App.tsx ever goes back to mount-on-view for the agent tab, both
        halves above become dead weight — fine, but then this file should be
        deleted with the mechanism, not left passing vacuously."""
        app = _read("App.tsx")
        self.assertIn("agentVisited", app,
                      "the keep-alive gate is gone — remove this guard with it")


if __name__ == "__main__":
    unittest.main()
