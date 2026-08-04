"""Shared helpers for the QA suite. Import only from inside tests/fixtures —
by then conftest.py has already pinned the environment."""
import time

from qa.env_recipe import QA_PASSWORD


def ensure_authed(client) -> None:
    """Log the TestClient in through the real front door. On a pristine home
    this exercises the first-run /setup flow; afterwards it is a plain login."""
    if client.get("/api/brand").status_code == 200:
        return  # cookie already valid
    from ava_bridge import auth
    if auth.needs_setup():
        r = client.post("/setup", data={"password": QA_PASSWORD,
                                        "confirm": QA_PASSWORD},
                        follow_redirects=False)
        assert r.status_code == 303, f"setup failed: {r.status_code} {r.text[:200]}"
        return  # /setup sets the session cookie itself
    r = client.post("/login", data={"password": QA_PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 303, f"login failed: {r.status_code} {r.text[:200]}"


def anon_client():
    """A cookie-less client on the same app (no lifespan re-run) for testing the
    unauthenticated view of the world."""
    from fastapi.testclient import TestClient
    import phone_bridge
    return TestClient(phone_bridge.app, follow_redirects=False,
                      client=("127.0.0.1", 50001), base_url="http://localhost")


def internal_headers(scope: str | None = None) -> dict:
    """The sandbox-side internal token header. `scope` picks a derived
    capability-group token instead of the root secret."""
    from ava_bridge import config, internal
    tok = internal._derived_token(scope) if scope else config.INTERNAL_TOKEN
    return {"X-Ava-Internal-Token": tok}


def ingest_bearer(cid: str) -> dict:
    from ava_bridge import internal
    return {"Authorization": "Bearer " + internal.ingest_token(cid)}


def wait_turn(client, tid: str, timeout: float = 30.0) -> dict:
    """Poll /api/turn/{tid} until it leaves 'running'. Returns the final turn."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/turn/{tid}")
        assert r.status_code == 200, r.text
        t = r.json()
        if t.get("status") != "running":
            return t
        time.sleep(0.2)
    raise AssertionError(f"turn {tid} still running after {timeout}s")


def refresh_connectors() -> None:
    """Force the live registry + perf sources to re-read right now (tests move
    faster than the 30s registry TTL)."""
    from ava_bridge import connectors, perf_mgmt
    connectors.load(force=True)
    perf_mgmt.refresh_sources()
