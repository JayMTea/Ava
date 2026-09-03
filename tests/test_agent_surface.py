"""The agent surface of one connector: `connectors.actions()`, `app_actions()`
and the `/api/apps/{cid}/actions` route behind the ActionConsole.

`actions()` knew two connector shapes -- static `actions:` and the ava-tools/1
`actions.discover` facade -- and never learned the third. A connector declaring
a real `mcp:` server reported NO actions at all, while `transport()` called it
"mcp", `render_egress_policy()` granted it __tools/__call and the agent called
its tools perfectly well. Five readers of one distinction; one was never taught
the pair.

It surfaced in the only place it could: an app with `ui.embed: none` has no UI
but the console, so a healthy 25-tool MCP app rendered "This app declares no
agent actions." and looked misconfigured to its owner.

Two guards, then, and the second is the one that generalises:

  * `actions()` counts an `mcp:` connector.
  * transport() and actions() may never disagree about whether a connector has
    an agent surface -- the invariant the original bug violated, and one that
    holds for whatever shape is added next as well as for these three.

The rest pin `app_actions()`, which answers what the manifest cannot: what the
app actually serves, where that list came from, and the tier Ava will enforce
for each tool -- as opposed to the tier the app claims for itself.

House style: stdlib unittest, no network -- discovery is mocked at its seam.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-surface-test-"))

from fastapi.testclient import TestClient

import phone_bridge
from ava_bridge import auth, config, connectors, tools_cache

# A real `mcp:` server. Modelled on the manifest that exposed the bug: no UI of
# its own, so the console is the whole tile.
MCP = {
    "id": "arqaid", "label": "Arqaid", "kind": "app",
    "mcp": {"url": "http://arqaid-mcp:9200/mcp", "token_env": "ARQAID_TOKEN"},
    "ui": {"label": "Arqaid", "icon": "chart", "section": "apps", "embed": "none"},
    "trust_declared_tiers": True,
}
# The ava-tools/1 HTTP facade -- MCP-shaped, but not MCP.
DISCOVER = {
    "id": "healthapp", "label": "Health App", "kind": "app",
    "actions": {"discover": {"base": "http://healthapp-api:8000",
                             "list": "/tools", "call": "/call"}},
}
# Statically declared actions proxied to the app's own REST API.
REST = {
    "id": "notes", "label": "Notes", "kind": "app",
    "actions": [
        {"id": "read_notes", "path": "/notes", "method": "GET"},
        # No `path:` -- served by a built-in Ava tool rather than the generic
        # proxy. Still something the agent can do, so the console must show it.
        {"id": "remember", "description": "a built-in memory write"},
    ],
}
# A UI-only app: an iframe tile and nothing for the agent at all.
UI_ONLY = {"id": "cams", "label": "Cameras", "kind": "app",
           "ui": {"embed": "iframe", "url": "http://cams:80"}}


def _surface(manifest, tools=None, error=None, cached=None, live=True,
             raises=None):
    """`app_actions` with discovery mocked at its seam."""
    disc = {"error": error} if error else {"tools": [] if tools is None else tools}
    kw = {"side_effect": raises} if raises else {"return_value": disc}
    with mock.patch.object(connectors, "load", return_value=[manifest]), \
         mock.patch.object(connectors, "discover_tools", **kw), \
         mock.patch.object(tools_cache, "for_connector", return_value=cached or {}):
        return connectors.app_actions(manifest["id"], live)


class ActionsCountsEveryDynamicShape(unittest.TestCase):
    def test_an_mcp_connector_reports_its_discovery_bridge_action(self):
        with mock.patch.object(connectors, "load", return_value=[MCP]):
            acts = [a for a in connectors.actions() if a["connector"] == "arqaid"]
        self.assertEqual([a["id"] for a in acts], ["arqaid"],
                         "an `mcp:` connector reported no actions at all")
        self.assertTrue(acts[0].get("dynamic"))

    def test_the_facade_still_reports_one_too(self):
        with mock.patch.object(connectors, "load", return_value=[DISCOVER]):
            acts = [a for a in connectors.actions() if a["connector"] == "healthapp"]
        self.assertEqual([a["id"] for a in acts], ["healthapp"])

    def test_transport_and_actions_never_disagree(self):
        """The generalising guard.

        `transport()` naming a wire protocol while `actions()` reports nothing
        is a contradiction: the first says there is an agent surface and the
        second says there is not. Whichever shape comes next, this holds.
        """
        for m in (MCP, DISCOVER, REST):
            with self.subTest(connector=m["id"]):
                with mock.patch.object(connectors, "load", return_value=[m]):
                    kind = connectors.transport(m)
                    acts = [a for a in connectors.actions()
                            if a["connector"] == m["id"]]
                self.assertNotEqual(kind, "none")
                self.assertTrue(acts,
                                f"transport() says {kind!r}, actions() says none")

    def test_a_connector_with_no_surface_still_reports_none(self):
        with mock.patch.object(connectors, "load", return_value=[UI_ONLY]):
            self.assertEqual(connectors.transport(UI_ONLY), "none")
            self.assertEqual([a for a in connectors.actions()
                              if a["connector"] == "cams"], [])


class AppActionsResolvesTheRealSet(unittest.TestCase):
    def test_a_live_answer_is_reported_as_live(self):
        s = _surface(MCP, tools=[{"name": "search_papers", "description": "Search"}])
        self.assertEqual(s["transport"], "mcp")
        self.assertEqual(s["source"], "live")
        self.assertIsNone(s["error"])
        self.assertEqual([t["name"] for t in s["tools"]], ["search_papers"])
        self.assertEqual(s["tools"][0]["description"], "Search")

    def test_an_app_answering_with_nothing_is_not_an_app_that_never_answered(self):
        """The distinction the console could not previously draw.

        Both render an empty list; they send the owner to entirely different
        places. `live` with [] means the app is fine and has no tools. `none`
        with an error means nobody knows what it has.
        """
        answered = _surface(MCP, tools=[])
        self.assertEqual((answered["source"], answered["error"]), ("live", None))

        silent = _surface(MCP, error="arqaid mcp: connection refused")
        self.assertEqual(silent["source"], "none")
        self.assertEqual(silent["tools"], [])
        self.assertIn("connection refused", silent["error"])

    def test_an_unreachable_app_falls_back_to_the_list_it_served_last(self):
        s = _surface(MCP, error="arqaid mcp: connection refused",
                     cached={"search_papers": {"description": "Search",
                                               "access": "read"}})
        self.assertEqual(s["source"], "cache")
        self.assertEqual([t["name"] for t in s["tools"]], ["search_papers"])
        self.assertIn("connection refused", s["error"],
                      "a stale list must carry why it could not be refreshed")

    def test_rows_carry_the_tier_ava_enforces_not_the_one_the_app_claims(self):
        """A permissions surface may not repeat an app's self-report as fact.

        The manifest's `dynamic_access` patterns are the operator's word and
        outrank the app's. An app under-claiming its own reach is exactly the
        case where echoing the claim would mislead.
        """
        m = {**MCP, "dynamic_access": {"search_*": "read", "*": "destructive"}}
        s = _surface(m, tools=[
            {"name": "search_papers", "access": "destructive"},   # under-claims
            {"name": "wipe_everything", "access": "read"},        # over-claims
        ])
        rows = {t["name"]: t for t in s["tools"]}
        self.assertEqual(rows["search_papers"]["access"], "read")
        self.assertFalse(rows["search_papers"]["confirm"],
                         "a read tool runs without asking")
        self.assertEqual(rows["wipe_everything"]["access"], "destructive")
        self.assertTrue(rows["wipe_everything"]["confirm"],
                        "a destructive tool asks every time")

    def test_a_static_connector_needs_no_network_and_keeps_pathless_actions(self):
        with mock.patch.object(connectors, "load", return_value=[REST]), \
             mock.patch.object(connectors, "discover_tools",
                               side_effect=AssertionError("must not discover")):
            s = connectors.app_actions("notes")
        self.assertEqual((s["transport"], s["source"]), ("rest", "declared"))
        self.assertEqual([t["name"] for t in s["tools"]], ["read_notes", "remember"])
        self.assertEqual(s["tools"][0]["access"], "read")   # GET infers read

    def test_a_ui_only_app_says_so_rather_than_reporting_an_outage(self):
        s = _surface(UI_ONLY)
        self.assertEqual((s["transport"], s["source"]), ("none", "none"))
        self.assertIn("declares no agent surface", s["error"])

    def test_live_false_takes_the_cached_path_without_asking_the_app(self):
        with mock.patch.object(connectors, "load", return_value=[MCP]), \
             mock.patch.object(connectors, "discover_tools",
                               side_effect=AssertionError("must not discover")), \
             mock.patch.object(tools_cache, "for_connector",
                               return_value={"search_papers": {"description": "S"}}):
            s = connectors.app_actions("arqaid", live=False)
        self.assertEqual(s["source"], "cache")

    def test_a_transport_that_raises_becomes_data_not_a_crash(self):
        s = _surface(MCP, raises=RuntimeError("boom"),
                     cached={"search_papers": {"description": "S"}})
        self.assertEqual(s["source"], "cache")
        self.assertIn("RuntimeError", s["error"])

    def test_an_unknown_connector_is_answered_not_raised(self):
        with mock.patch.object(connectors, "load", return_value=[]):
            s = connectors.app_actions("ghost")
        self.assertEqual((s["transport"], s["source"]), ("none", "none"))
        self.assertIn("unknown connector", s["error"])


class TiersAreRightOnTheFirstRender(unittest.TestCase):
    """The FIRST discovery must already label tiers correctly.

    `action_access` reads a dynamic tool's self-reported tier out of
    `tools_cache`, which is filled by discovery's own write-through. So this
    only works because `discover_tools` calls `_remember()` before returning --
    the cache is warm by the time `app_actions` builds its rows.

    Move that write later and nothing fails loudly: the first time an owner
    opens the tile, every tool renders `write / asks first`, including the
    read-only ones, and the panel quietly overstates what the app can do. A
    permissions surface that is wrong only on first view is worse than one that
    is wrong always, because nobody reproduces it.

    Mocked at the socket (`mcp_client.list_tools`), so the real
    app_actions -> discover_tools -> _mcp_spec chain runs.
    """

    def test_the_write_through_lands_before_the_rows_are_built(self):
        from ava_bridge import mcp_client

        cid = "arqaid-firstrender"
        m = {**MCP, "id": cid}
        # Shaped as mcp_client.normalise_tools() delivers it: a top-level
        # `access`, which is the one field tools_cache.update reads.
        served = {"tools": [
            {"name": "search_papers", "description": "Search.", "access": "read"},
            {"name": "ingest_source", "description": "Ingest.", "access": "write"},
        ]}
        self.addCleanup(tools_cache.forget, cid)
        tools_cache.forget(cid)
        self.assertEqual(tools_cache.for_connector(cid), {},
                         "precondition: this connector has never been discovered")

        with mock.patch.object(connectors, "load", return_value=[m]), \
             mock.patch.object(mcp_client, "list_tools", return_value=served):
            s = connectors.app_actions(cid)

        rows = {t["name"]: t for t in s["tools"]}
        self.assertEqual(s["source"], "live")
        self.assertEqual(rows["search_papers"]["access"], "read")
        self.assertFalse(rows["search_papers"]["confirm"],
                         "a read tool was labelled as one that asks first -- the "
                         "tier cache was cold when the rows were built")
        self.assertEqual(rows["ingest_source"]["access"], "write")
        self.assertTrue(rows["ingest_source"]["confirm"])


def _authed() -> TestClient:
    c = TestClient(phone_bridge.app, base_url="http://localhost")
    c.cookies.set(config.COOKIE_NAME, auth._make_token())
    return c


class ActionsRoute(unittest.TestCase):
    def test_it_serves_names_for_compatibility_and_rows_for_the_console(self):
        with mock.patch.object(connectors, "load", return_value=[MCP]), \
             mock.patch.object(connectors, "discover_tools", return_value={
                 "tools": [{"name": "search_papers", "description": "Search"}]}):
            r = _authed().get("/api/apps/arqaid/actions")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["actions"], ["search_papers"])
        self.assertEqual(body["tools"][0]["name"], "search_papers")
        self.assertEqual(body["transport"], "mcp")
        self.assertEqual(body["source"], "live")
        self.assertIsNone(body["error"])

    def test_an_unreachable_app_answers_200_with_the_reason(self):
        """A 500 here is a blank panel with nothing to read, and this console is
        the only view the app has."""
        with mock.patch.object(connectors, "load", return_value=[MCP]), \
             mock.patch.object(connectors, "discover_tools",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(tools_cache, "for_connector", return_value={}):
            r = _authed().get("/api/apps/arqaid/actions")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["actions"], [])
        self.assertIn("RuntimeError", r.json()["error"])

    def test_live_0_skips_the_network(self):
        with mock.patch.object(connectors, "load", return_value=[MCP]), \
             mock.patch.object(connectors, "discover_tools",
                               side_effect=AssertionError("must not discover")), \
             mock.patch.object(tools_cache, "for_connector", return_value={}):
            r = _authed().get("/api/apps/arqaid/actions?live=0")
        self.assertEqual(r.status_code, 200)

    def test_an_unknown_app_is_a_404(self):
        with mock.patch.object(connectors, "load", return_value=[]):
            r = _authed().get("/api/apps/ghost/actions")
        self.assertEqual(r.status_code, 404)

    def test_it_is_cookie_gated_like_every_other_api_route(self):
        """The tool names of the owner's apps are not public."""
        with mock.patch.object(connectors, "load", return_value=[MCP]):
            r = TestClient(phone_bridge.app, base_url="http://localhost").get(
                "/api/apps/arqaid/actions")
        self.assertIn(r.status_code, (401, 403, 302, 307))


if __name__ == "__main__":
    unittest.main()
