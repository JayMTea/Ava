"""The SSE transport must clean up after a FAILED connect.

`_SseSession.__init__` opens real resources before it can know the connect
worked: the long-lived GET stream (a socket) and the daemon reader thread. A
server that answers 200 with a well-formed SSE stream but never announces its
`endpoint` event — e.g. an app whose /sse is a plain keepalive stream — made
__init__ raise AFTER both existed, and the exception path dropped the
half-built session without closing anything. Nothing else could: the instance
was never returned, so there was no registry entry and nobody to call close().
One socket and one reader thread leaked per attempt, forever — and the Hub
retries this path on every visit.

The fix routes every constructor failure through close() (which shutdown()s
the socket, popping the reader out of its blocking read so the thread exits),
plus a `_make_session` backstop that tears down any transport whose __init__
raises. These tests run real failed connects against a live endpoint-less SSE
server and assert the process returns to baseline: no `mcp-sse-*` threads, no
session registry entry, no ResourceWarning from a dropped-but-open response.
"""
import gc
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ava_bridge import mcp_client

ATTEMPTS = 3


class _EndpointlessSse(BaseHTTPRequestHandler):
    """Speaks SSE (200, right content type, keepalive comments) but never sends
    the `endpoint` event — the exact shape the leak was reproduced against."""

    stop = threading.Event()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            while not self.stop.is_set():
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass  # the client shut the socket down — that IS the fix working

    def log_message(self, *a):  # keep pytest output clean
        pass


@pytest.fixture()
def sse_url(monkeypatch):
    # Shorten the endpoint wait — the same code path, just bounded for a test.
    monkeypatch.setattr(mcp_client, "_STDIO_START_TIMEOUT", 1)
    _EndpointlessSse.stop.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _EndpointlessSse)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/sse"
    finally:
        mcp_client.reset()
        _EndpointlessSse.stop.set()
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _spec(url):
    return {"transport": "sse", "url": url, "command": None, "env": None,
            "token_env": None}


def _sse_threads():
    return [t for t in threading.enumerate() if t.name.startswith("mcp-sse")]


def _settles(predicate, deadline=10.0):
    end = time.time() + deadline
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_a_failed_connect_reports_an_error_not_an_exception(sse_url):
    out = mcp_client.list_tools("leaktest", _spec(sse_url))
    assert "error" in out, out
    assert "endpoint" in out["error"], out["error"]


def test_reader_threads_and_sockets_do_not_accumulate(sse_url):
    baseline = threading.active_count()
    assert _sse_threads() == []
    for _ in range(ATTEMPTS):
        out = mcp_client.list_tools("leaktest", _spec(sse_url))
        assert "error" in out, out
    # The reader threads must EXIT — daemonic is not cleanup, it is deferral.
    assert _settles(lambda: not _sse_threads()), (
        f"{len(_sse_threads())} mcp-sse reader thread(s) survived {ATTEMPTS} "
        "failed connect attempts — the stream was never torn down")
    # And the process settles back to its pre-test thread count: the fake
    # server's per-connection handler threads exit too, once the client half
    # of each socket is shut down.
    assert _settles(lambda: threading.active_count() <= baseline), (
        f"thread count stuck at {threading.active_count()} > baseline {baseline}")


def test_no_session_is_registered_for_a_failed_connect(sse_url):
    for _ in range(ATTEMPTS):
        mcp_client.list_tools("leaktest", _spec(sse_url))
    assert "leaktest" not in mcp_client._sessions, (
        "a session that never produced an endpoint must not be cached — the "
        "next attempt would reuse a dead transport")


def test_no_resourcewarning_after_failed_connects(sse_url):
    """A leaked response socket surfaces as ResourceWarning («unclosed
    <socket…>») when the dropped session object is garbage-collected."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(ATTEMPTS):
            mcp_client.list_tools("leaktest", _spec(sse_url))
        _settles(lambda: not _sse_threads())
        gc.collect()
        gc.collect()
        leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert not leaks, (
        f"unclosed resources survived: {[str(w.message) for w in leaks]}")
