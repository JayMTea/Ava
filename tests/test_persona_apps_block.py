"""The persona's connected-apps block: derived at render time, never shipped.

`agent/persona.txt.tmpl` tells the model about `get_weather` and nothing about
the apps the owner has wired in — so "what did I eat today" went to whichever
tool the model guessed, or to none. `{{APPS_BLOCK}}` fixes that with one line
per app (label, how it is reached, which tool names it offers) rendered from
`ava_bridge.connectors.agent_surface()` and the tool names in
`ava_bridge.tools_cache` — at provision time, on the owner's box.

Three properties, each with a test:

* DERIVED. Nothing about anyone's apps is in the tracked template; a fork with
  no app connectors renders a persona with no apps line at all, so
  tests/test_no_owner_identity.py and tests/test_persona_neutral.py hold.
* HONEST. A static app is described by its native `<id>_<action>` tools, a
  dynamic (discover / MCP) one by its `<id>_find_tool` -> `<id>_call` pair and
  the names it actually reported — never a shape the sandbox does not hold.
* BOUNDED. The persona crosses an argv boundary base64'd (see STYLE_MAX); an
  app with hundreds of tools must not turn provisioning into an E2BIG.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from ava_bridge import connectors, tools_cache
from test_persona_neutral import _STYLISTIC

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _render_persona_module():
    """Load agent/render_persona.py by path, the way provision.py does — it is
    a script, not a package module."""
    path = ROOT / "agent" / "render_persona.py"
    spec = importlib.util.spec_from_file_location("ava_render_persona_under_test",
                                                  str(path))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


RP = _render_persona_module()


def _meta(cid="mycrm", label="My CRM", tools=(), discovered=True):
    return {"id": cid, "label": label, "meta": True,
            "tools": list(tools), "discovered": discovered}


def _native(cid="mycrm", label="My CRM", tools=("create_lead", "list_leads")):
    return {"id": cid, "label": label, "meta": False,
            "tools": list(tools), "discovered": False}


class AppsBlockFormatterTests(unittest.TestCase):
    """`apps_block` is pure: rows in, one paragraph out."""

    def test_no_apps_renders_nothing_at_all(self):
        # The whole fork-neutrality argument rests on this being "" and not a
        # heading with nothing under it.
        self.assertEqual(RP.apps_block([]), "")
        self.assertEqual(RP.apps_block([{"id": "", "label": "x"}]), "")
        self.assertEqual(RP.apps_block(["junk", None, 3]), "")

    def test_a_dynamic_app_is_reached_through_its_find_and_call_pair(self):
        out = RP.apps_block([_meta(tools=["list_leads", "create_lead"])])
        self.assertIn("My CRM", out)
        self.assertIn("mycrm_find_tool", out)
        self.assertIn("mycrm_call", out)
        self.assertIn("list_leads", out)
        self.assertIn("create_lead", out)
        # The names are the app's own — NOT prefixed as if they were native.
        self.assertNotIn("mycrm_list_leads", out)

    def test_a_static_app_is_reached_through_native_prefixed_tools(self):
        out = RP.apps_block([_native()])
        self.assertIn("mycrm_create_lead", out)
        self.assertIn("mycrm_list_leads", out)
        # No meta pair is claimed for it (the closing directive still explains
        # find_tool/call pairs in general — that sentence is app-agnostic).
        self.assertNotIn("mycrm_find_tool", out)
        self.assertNotIn("mycrm_call", out)

    def test_a_dynamic_app_never_discovered_says_search_first(self):
        # Empty until the first discovery is honest; inventing names is not.
        out = RP.apps_block([_meta(tools=[])])
        self.assertIn("mycrm_find_tool", out)
        self.assertIn("discovered live", out)

    def test_tool_names_are_capped_per_app_and_the_rest_counted(self):
        names = [f"tool_{i:03d}" for i in range(RP.APPS_TOOLS_MAX + 7)]
        out = RP.apps_block([_meta(tools=names)])
        self.assertIn(names[RP.APPS_TOOLS_MAX - 1], out)
        self.assertNotIn(names[RP.APPS_TOOLS_MAX], out)
        self.assertIn("and 7 more", out)
        out_native = RP.apps_block([_native(tools=names)])
        self.assertIn("and 7 more", out_native)

    def test_the_whole_block_is_capped_at_an_app_boundary(self):
        apps = [_meta(cid=f"app{i:03d}", label=f"App {i}",
                      tools=[f"t{i}_{j}" for j in range(RP.APPS_TOOLS_MAX)])
                for i in range(200)]
        out = RP.apps_block(apps)
        # Generous: the cap is on the app entries; the intro and the closing
        # directive ride on top and are a few hundred characters.
        self.assertLess(len(out), RP.APPS_MAX + 1500, len(out))
        self.assertIn("more app(s) are connected", out)
        # No entry was cut mid-name: every tool name that appears is whole.
        for token in out.replace(",", " ").replace(".", " ").split():
            if token.startswith("t") and "_" in token and token[1:4].isdigit():
                self.assertRegex(token, r"^t\d+_\d+$")

    def test_manifest_text_cannot_splice_a_template_block(self):
        # Labels and tool names come from files the owner edits; the same brace
        # hazard clean_owner_text documents for persona.style applies.
        out = RP.apps_block([_meta(label="Evil {{ADULT_BLOCK}} app",
                                   tools=["ok", "bad{{STYLE_BLOCK}}"])])
        self.assertNotIn("{{", out)
        self.assertNotIn("}}", out)

    def test_a_multiline_label_is_flattened(self):
        out = RP.apps_block([_native(label="Two\nlines")])
        self.assertNotIn("\n", out)
        self.assertIn("Two lines", out)

    def test_the_operational_directive_names_the_owner(self):
        out = RP.apps_block([_native()], user="Bob", user_poss="Bob's")
        self.assertIn("Bob's own apps", out)
        self.assertIn("tell Bob", out)
        # The three mandates the block exists to carry.
        self.assertIn("search first", out)
        self.assertIn("never retry", out)
        self.assertIn("Never fabricate", out)

    def test_the_block_carries_no_stylistic_opinion(self):
        """Same bar as the template: operational, never taste."""
        out = RP.apps_block([_meta(tools=["a", "b"]), _native(cid="other")]).lower()
        leaked = sorted({p for p in _STYLISTIC if p in out})
        self.assertEqual(leaked, [])


def _write(base, cid, body):
    d = os.path.join(base, cid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "connector.yaml"), "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))


class AgentSurfaceTests(unittest.TestCase):
    """`connectors.agent_surface()` — the rows the block is rendered from."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.user = os.path.join(self.tmp, "user")
        self.builtin = os.path.join(self.tmp, "builtin")
        os.makedirs(self.user)
        os.makedirs(self.builtin)
        self._p = [
            mock.patch.object(connectors, "BUILTIN_DIR", self.builtin),
            mock.patch.object(connectors, "USER_DIR", self.user),
            mock.patch.object(tools_cache, "PATH",
                              os.path.join(self.tmp, "connector_tools_cache.json")),
        ]
        for p in self._p:
            p.start()
        tools_cache._cache.update(data=None, mtime=0.0)

    def tearDown(self):
        for p in self._p:
            p.stop()
        tools_cache._cache.update(data=None, mtime=0.0)
        connectors.load(force=True)

    def _surface(self):
        connectors.load(force=True)
        return {r["id"]: r for r in connectors.agent_surface()}

    def test_a_small_static_app_lists_its_native_actions_in_declared_order(self):
        _write(self.user, "crm", """
            label: My CRM
            kind: app
            base_url: "http://127.0.0.1:9000"
            actions:
              static:
                - { id: zeta, method: GET, path: "/z" }
                - { id: alpha, method: POST, path: "/a" }
                - { id: no_path_so_no_tool, method: GET }
        """)
        row = self._surface()["crm"]
        self.assertFalse(row["meta"])
        self.assertFalse(row["discovered"])
        self.assertEqual(row["tools"], ["zeta", "alpha"])
        self.assertEqual(row["label"], "My CRM")

    def test_a_discover_app_reports_what_its_cache_holds_sorted(self):
        _write(self.user, "hello", """
            label: Hello App
            kind: app
            actions:
              discover: { base: "http://127.0.0.1:9001", list: "/tools", call: "/call" }
        """)
        tools_cache.update("hello", [{"name": "make_thing", "access": "write"},
                                     {"name": "list_things", "access": "read"}])
        row = self._surface()["hello"]
        self.assertTrue(row["meta"])
        self.assertTrue(row["discovered"])
        self.assertEqual(row["tools"], ["list_things", "make_thing"])

    def test_an_mcp_app_before_first_discovery_has_no_names_yet(self):
        _write(self.user, "srv", """
            label: MCP Server
            kind: app
            mcp: { url: "http://127.0.0.1:9200/mcp" }
        """)
        row = self._surface()["srv"]
        self.assertTrue(row["meta"])
        self.assertEqual(row["tools"], [])

    def test_a_large_static_app_switches_to_the_meta_pair(self):
        acts = "\n".join(f"                - {{ id: a{i}, method: GET, path: \"/{i}\" }}"
                         for i in range(connectors.META_TOOLS_MIN))
        _write(self.user, "big", f"""
            label: Big
            kind: app
            base_url: "http://127.0.0.1:9000"
            actions:
              static:
{acts}
        """)
        row = self._surface()["big"]
        self.assertTrue(row["meta"], "at META_TOOLS_MIN the tools are find/call")
        self.assertFalse(row["discovered"], "…but the names are the manifest's")
        self.assertEqual(len(row["tools"]), connectors.META_TOOLS_MIN)

    def test_ui_label_wins_over_manifest_label(self):
        _write(self.user, "crm", """
            label: crm-internal
            kind: app
            ui: { label: Customers, embed: none }
            base_url: "http://127.0.0.1:9000"
            actions:
              static:
                - { id: list, method: GET, path: "/l" }
        """)
        self.assertEqual(self._surface()["crm"]["label"], "Customers")

    def test_connectors_the_agent_cannot_reach_are_left_out(self):
        _write(self.user, "core-thing", """
            label: Core
            kind: core
            base_url: "http://127.0.0.1:9000"
            actions:
              static:
                - { id: list, method: GET, path: "/l" }
        """)
        _write(self.user, "ui-only", """
            label: UI Only
            kind: app
            ui: { embed: iframe, url: "http://127.0.0.1:9002" }
        """)
        _write(self.user, "switched-off", """
            label: Off
            kind: app
            enabled: false
            mcp: { url: "http://127.0.0.1:9200/mcp" }
        """)
        _write(self.user, "live", """
            label: Live
            kind: app
            mcp: { url: "http://127.0.0.1:9201/mcp" }
        """)
        self.assertEqual(sorted(self._surface()), ["live"])


class RenderedPersonaTests(unittest.TestCase):
    """The block in situ: through `render()` and through the real script."""

    def _render_with_empty_registry(self, surface_error: Exception | None = None):
        tmp = tempfile.mkdtemp()
        empty_a, empty_b = os.path.join(tmp, "a"), os.path.join(tmp, "b")
        os.makedirs(empty_a)
        os.makedirs(empty_b)
        with mock.patch.object(connectors, "BUILTIN_DIR", empty_a), \
             mock.patch.object(connectors, "USER_DIR", empty_b), \
             mock.patch.object(tools_cache, "PATH", os.path.join(tmp, "c.json")):
            tools_cache._cache.update(data=None, mtime=0.0)
            connectors.load(force=True)
            try:
                if surface_error is not None:
                    with mock.patch.object(connectors, "agent_surface",
                                           side_effect=surface_error):
                        return RP.render()
                return RP.render()
            finally:
                tools_cache._cache.update(data=None, mtime=0.0)
                connectors.load(force=True)

    def test_a_fork_with_no_apps_gets_the_persona_it_always_had(self):
        out = self._render_with_empty_registry()
        self.assertNotIn("Connected apps", out)
        self.assertNotIn("connector id", out)
        self.assertNotIn("{{", out)
        self.assertIn("get_weather", out)
        # Byte-for-byte: the placeholder leaves no stray space behind.
        self.assertIn("if a tool of yours can do it, do it. Formatting:", out)

    def test_a_registry_failure_costs_the_apps_line_not_the_provision(self):
        """agent/install.sh runs the render under `set -euo pipefail`."""
        out = self._render_with_empty_registry(RuntimeError("manifest exploded"))
        self.assertIn("get_weather", out)
        self.assertNotIn("Connected apps", out)
        self.assertNotIn("{{", out)

    def test_the_script_renders_an_app_from_a_manifest_and_its_cached_tools(self):
        """Through the real entry point, the way agent/install.sh runs it, with
        AVA_HOME pointing at a throwaway tree: a manifest under connectors/ and
        a seeded tools cache beside it."""
        home = tempfile.mkdtemp()
        _write(os.path.join(home, "connectors"), "hello-app", """
            label: Hello App
            kind: app
            actions:
              discover: { base: "http://127.0.0.1:9001", list: "/tools", call: "/call" }
        """)
        with open(os.path.join(home, "connector_tools_cache.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"hello-app": {
                "list_things": {"access": "read", "description": "List."},
                "make_thing": {"access": "write", "description": "Make."}}}, f)
        with open(os.path.join(home, "ava.yaml"), "w", encoding="utf-8") as f:
            f.write("owner:\n  name: Sam\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("AVA_PERSONA")}
        env["AVA_HOME"] = home
        r = subprocess.run([sys.executable, str(ROOT / "agent" / "render_persona.py")],
                           capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertIn("Hello App (connector id hello-app)", out)
        self.assertIn("hello-app_find_tool", out)
        self.assertIn("hello-app_call", out)
        self.assertIn("list_things, make_thing", out)
        self.assertIn("Sam's own apps", out)
        self.assertIn("tell Sam", out)
        self.assertNotIn("{{", out)
        # One paragraph, as the template is: nothing on the way in printed a
        # stray line into the prompt.
        self.assertEqual(out.count("\n"), 0, out[:200])
        self.assertLess(len(out), 20_000)


if __name__ == "__main__":
    unittest.main()
