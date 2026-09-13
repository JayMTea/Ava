"""Native app schemas keep the existing dynamic-call consent boundary."""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import httpx

from ava_bridge import connectors, tools_cache


class NativeTools(unittest.TestCase):
    def test_selected_actions_use_discovered_schemas_and_match_routing(self):
        manifest = {"id": "demo", "kind": "app", "label": "Demo",
                    "actions": {"discover": {"base": "http://example.test"}},
                    "agent_tools": ["search_catalog"]}
        schema = {"type": "object", "properties": {"q": {"type": "string"}},
                  "additionalProperties": False}
        with mock.patch.object(connectors, "load", return_value=[manifest]), \
             mock.patch.object(tools_cache, "for_connector", return_value={
                 "search_catalog": {"description": "Find datasets", "inputSchema": schema}}):
            files = connectors.tool_files("demo")
            self.assertEqual([f["name"] for f in files], ["demo_search_catalog.mjs"])
            self.assertEqual(connectors._tool_file_names(manifest), [{"name": files[0]["name"]}])
            self.assertFalse(connectors.agent_surface()[0]["meta"])
            self.assertIn(json.dumps(schema), files[0]["source"])
        with mock.patch.object(connectors, "load", return_value=[manifest]), \
             mock.patch.object(tools_cache, "for_connector", return_value={}):
            with self.assertRaisesRegex(ValueError, "Discover app tools"):
                connectors.tool_files("demo")

    def test_names_are_bounded_and_cannot_be_paths(self):
        for names in [["../bad"], ["a", "a"], [{}], ["a"] * 17]:
            with self.subTest(names=names), self.assertRaises(ValueError):
                connectors._native_tool_names({"agent_tools": names,
                    "actions": {"discover": {"base": "http://example.test"}}})

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_native_handler_preserves_action_and_authentication(self):
        source = connectors.render_native_tool("demo", "measure", {
            "description": "Owner's measure\nwith a source", "inputSchema": {
                "type": "object", "properties": {"year": {"type": "integer"}}}})
        harness = """import tool from './tool.mjs';
let captured;
const ctx = {internalToken:'test-token', http:{postJson:async (...args)=>{
  captured=args;return {structuredContent:{ava_artifact_id:'recorded'},
    content:[{type:'text',text:'Duplicated transport envelope'}],app_status:200};
}}};
const result=await tool.handler({year:2024},ctx);
console.log(JSON.stringify({captured,result:JSON.parse(result)}));
"""
        with tempfile.TemporaryDirectory() as directory:
            for name, body in [("tool.mjs", source), ("run.mjs", harness)]:
                with open(os.path.join(directory, name), "w", encoding="utf-8") as stream:
                    stream.write(body)
            result = subprocess.run(["node", "run.mjs"], cwd=directory,
                                    capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        url, body, options = data["captured"]
        self.assertTrue(url.endswith("/internal/connector/demo/__call"))
        self.assertEqual(body, {"name": "measure", "arguments": {"year": 2024}})
        self.assertEqual(options["headers"]["X-Ava-Internal-Token"], "test-token")
        self.assertFalse(options["direct"])
        self.assertEqual(data["result"], {"ava_artifact_id": "recorded"})


class RuntimeConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_activity_exec_remains_responsive_during_a_turn(self):
        from ava_bridge import agent_runtime_server as server
        started, release = threading.Event(), threading.Event()

        def run(*args, **kwargs):
            started.set()
            release.wait(2)
            return "done", []

        with mock.patch.object(server.config, "AGENT_TOKEN", "test-token"), \
             mock.patch.object(server._rt, "run_turn", side_effect=run), \
             mock.patch.object(server._rt, "exec", return_value="activity"):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                    base_url="http://agent", headers={"X-Ava-Agent-Token": "test-token"}) as client:
                turn = asyncio.create_task(client.post("/run_turn", json={"text": "test"}))
                try:
                    for _ in range(100):
                        if started.is_set():
                            break
                        await asyncio.sleep(.01)
                    self.assertTrue(started.is_set())
                    response = await asyncio.wait_for(client.post("/exec", json={"inner": "read"}), .5)
                    self.assertEqual(response.json(), {"out": "activity"})
                    self.assertFalse(turn.done(), "the long turn blocked activity polling")
                finally:
                    release.set()
                    await turn
