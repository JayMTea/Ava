"""The requirements-pin canary: prefixed APIRouters must actually route.

fastapi 0.138+/0.139 with starlette 1.3.x silently drops every route on a
prefixed APIRouter — include_router succeeds, docs look fine, but requests 404.
That killed the whole /api/hub/* Hub API once (see the pin comment in
requirements.txt) and CI was green because nothing exercised routing through
HTTP. This does. If a dependency bump turns these red, the combo is still
broken — keep the pin (and the dependabot ignore in .github/dependabot.yml).
"""
import unittest

from fastapi import APIRouter, FastAPI


def _paths(routes, prefix=""):
    """Every resolvable path, recursing into nested mounts (starlette 1.3
    nests included routers, so a flat app.routes scan lies)."""
    out = []
    for r in routes:
        p = prefix + getattr(r, "path", "")
        sub = getattr(r, "routes", None)
        out += _paths(sub, p) if sub else [p]
    return out


class PrefixedRouterCanary(unittest.TestCase):
    def test_minimal_prefixed_router_serves(self):
        r = APIRouter(prefix="/x")

        @r.get("/a")
        def a():
            return {"ok": True}

        app = FastAPI()
        app.include_router(r)
        self.assertIn("/x/a", _paths(app.routes),
                      "prefixed APIRouter routes were silently dropped — the "
                      "fastapi/starlette combo is broken; keep the pins")
        from starlette.testclient import TestClient
        self.assertEqual(TestClient(app, base_url="http://localhost").get("/x/a").status_code, 200)

    def test_hub_router_mounts_and_serves(self):
        # The real casualty last time: every /api/hub/* route vanished.
        from ava_bridge.hub_api import router
        app = FastAPI()
        app.include_router(router)
        self.assertIn("/api/hub/approvals", _paths(app.routes))
        # approvals_list has no config/filesystem dependencies — a clean probe.
        from starlette.testclient import TestClient
        resp = TestClient(app, base_url="http://localhost").get("/api/hub/approvals")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("pending", resp.json())


if __name__ == "__main__":
    unittest.main()
