"""A coded error on `/internal/*` must arrive as HTTP 200, or Ava never sees it.

Every sandbox tool reaches the bridge through `agent/mcp_server_*/_lib.mjs`,
which calls `curl --fail`. `--fail` makes curl exit 22 on any non-2xx and print
NOTHING of the body — so a handler that returns
`400 {"error": "Cannot modify protected policy"}` reaches the tool as
`request failed: exit status 22`, and Ava tells the owner nothing at all.

CLAUDE.md states the rule. Five routes followed it and said so in their comments;
twenty-odd others returned 4xx/5xx, including the policy-management route whose
entire purpose was explaining why an edit was refused. Nothing enforced it, which
is the only reason that could drift so far — so this enforces it.

401 is the deliberate exception: an unauthenticated caller is not Ava, there is
no one to explain anything to, and `auth.auth_gate` already rejects before
routing. It is allow-listed by status, not by route, so a new handler cannot
quietly join it.

Same shape as tests/test_diagram_sync.py: a static scan plus a live check, no
sandbox and no network.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INTERNAL = ROOT / "ava_bridge" / "internal.py"

#: The only status a handler on this surface may return other than 200.
ALLOWED_NON_200 = {401}


class StaticContractTests(unittest.TestCase):

    def test_no_handler_returns_a_body_the_sandbox_will_discard(self):
        tree = ast.parse(INTERNAL.read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "JSONResponse"):
                continue
            for kw in node.keywords:
                if kw.arg != "status_code":
                    continue
                if isinstance(kw.value, ast.Constant) and \
                        kw.value.value not in ALLOWED_NON_200 | {200}:
                    offenders.append(f"internal.py:{node.lineno} -> {kw.value.value}")
                elif not isinstance(kw.value, ast.Constant):
                    # A computed status is the shape that caused this: a
                    # `200 if ok else 400` reads as careful and is not.
                    offenders.append(f"internal.py:{node.lineno} -> computed status")
        self.assertFalse(offenders, (
            "these return a non-200 on /internal/*, so the sandbox helper's "
            f"`curl --fail` throws the body away before Ava sees it: {offenders}. "
            "Use internal._told(message, code), which answers 200 with "
            "{'error', 'error_code'}."))

    def test_the_sandbox_helper_still_uses_curl_fail(self):
        """If this ever stops being true the rule above can be relaxed — but it
        has to be relaxed deliberately, not discovered."""
        lib = (ROOT / "agent" / "mcp_server_content" / "_lib.mjs").read_text(encoding="utf-8")
        self.assertIn("--fail", lib,
                      "the helper no longer passes --fail; re-check whether "
                      "/internal/* still needs the HTTP-200 convention")

    def test_coded_errors_carry_a_code_not_just_prose(self):
        """`error_code` is what the frontend pattern-matches on and what Ava can
        relay verbatim. A bare `error` string is a dead end."""
        src = INTERNAL.read_text(encoding="utf-8")
        self.assertIn('"error_code"', src)
        self.assertIn("def _told(", src)


class LiveContractTests(unittest.TestCase):
    """The routes themselves, driven with a real token."""

    def setUp(self) -> None:
        from ava_bridge import config, internal
        self.internal = internal
        self.headers = {"x-ava-internal-token": config.INTERNAL_TOKEN}

    def _get(self, path: str, **params):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(self.internal.router)
        return TestClient(app).get(path, headers=self.headers, params=params)

    def test_a_refused_request_is_still_a_200_with_a_code(self):
        r = self._get("/internal/learning/chats", action="nonsense")
        self.assertEqual(r.status_code, 200,
                         "the sandbox would never see this body")
        self.assertTrue(r.json().get("error"))
        self.assertTrue(r.json().get("error_code"))

    def test_an_unauthenticated_caller_still_gets_401(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(self.internal.router)
        r = TestClient(app).get("/internal/documents",
                                headers={"x-ava-internal-token": "nope"})
        self.assertEqual(r.status_code, 401)


class ModelRouteTests(unittest.TestCase):
    """`/internal/model` had a scope, auth tests, an egress grant and a shipped
    tool calling it — and no handler."""

    def test_the_route_exists(self):
        from ava_bridge import internal

        paths = {r.path for r in internal.router.routes}
        self.assertIn("/internal/model", paths,
                      "agent/mcp_server_productivity/productivity/"
                      "get_active_model.mjs calls this on every invocation")

    def test_it_reads_the_one_resolver_rather_than_deriving_its_own(self):
        import inspect

        from ava_bridge import internal

        src = inspect.getsource(internal.internal_model)
        self.assertIn("effective_brain", src,
                      "a fourth independent derivation of which model is live is "
                      "how the model chip came to disagree with reality")

    def test_it_separates_configured_from_answering(self):
        from unittest import mock

        from ava_bridge import internal

        with mock.patch("ava_bridge.models.effective_brain",
                        return_value={"model_id": "m", "label": "M", "engine": "e",
                                      "source": "backend", "local": True,
                                      "implicit": False}), \
             mock.patch("ava_bridge.agent.which_model", return_value=None):
            body = internal.internal_model(_AuthedRequest())
        self.assertEqual(body["configured"]["model"], "m")
        self.assertIsNone(body["answering"],
                          "nothing has answered yet — a real state, not an error")


class _AuthedRequest:
    """Minimal stand-in: `authorized()` only reads one header."""

    def __init__(self) -> None:
        from ava_bridge import config
        self.headers = {"x-ava-internal-token": config.INTERNAL_TOKEN}


if __name__ == "__main__":
    unittest.main()
