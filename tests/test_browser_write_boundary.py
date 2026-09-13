"""An embedded app must never approve an agent action using the owner's cookie."""
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ava_bridge import auth, apps_origin


def test_cookie_authenticated_cross_origin_write_is_blocked_before_routing():
    app = FastAPI()
    app.middleware("http")(auth.auth_gate)
    calls = []

    @app.post("/api/hub/approvals/example")
    def approve():
        calls.append(True)
        return {"ok": True}

    with patch.object(auth, "is_authed", return_value=True), \
         patch.object(auth, "host_is_trusted", return_value=(True, "")), \
         patch.object(auth, "needs_renewal", return_value=False), \
         patch.object(apps_origin, "configured", return_value=None), TestClient(app) as client:
        for headers in [{"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Origin": "http://testserver:10443"}, {"Origin": "null"}]:
            assert client.post("/api/hub/approvals/example", headers=headers).status_code == 403
        assert calls == []
        assert client.post("/api/hub/approvals/example", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
        assert calls == [True]
