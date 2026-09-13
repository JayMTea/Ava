import asyncio

from starlette.requests import Request
from starlette.responses import Response


def test_controller_peer_filter_cannot_be_bypassed_with_forwarded_header(monkeypatch):
    from ava_bridge import agent_runtime_server as shim
    monkeypatch.setenv("AVA_AGENT_ALLOWED_IPS", "192.0.2.10/32")
    reached = []

    async def next_handler(request):
        reached.append(True)
        return Response(status_code=204)

    def check(peer):
        request = Request({"type": "http", "method": "GET", "scheme": "http",
                           "path": "/healthz", "query_string": b"",
                           "headers": [(b"x-forwarded-for", b"192.0.2.10")],
                           "server": ("controller", 9100), "client": (peer, 1234)})
        return asyncio.run(shim._auth(request, next_handler)).status_code

    assert check("192.0.2.20") == 403
    assert reached == []
    assert check("192.0.2.10") == 204


def test_runtime_uses_authenticated_openshell_exec_when_configured(monkeypatch):
    from ava_bridge.runtime.nemoclaw import NemoClawRuntime
    monkeypatch.setenv("AVA_SANDBOX_EXEC_MODE", "openshell")
    monkeypatch.setenv("AVA_OPENSHELL", "/operator/openshell")
    runtime = NemoClawRuntime()
    command = runtime._base("exec", "--no-tty", "--", "id")
    assert command == ["/operator/openshell", "sandbox", "exec", "--name", runtime.sandbox,
                       "--no-tty", "--", "id"]
