"""A connector that is DOWN must not read like a connector that is BROKEN.

Both used to render the same way. Every failed hop to a connector formatted the
raw exception, so an app that simply was not running produced

    {"error": "device-app app unreachable: HTTPConnectionPool(host='127.0.0.1',
     port=8481): Max retries exceeded with url: /?theme=dark&embedded=1&v=1
     (Caused by NewConnectionError(...Failed to establish a new connection:
     [Errno 111] Connection refused))"}

— the library, its retry policy and the query string, but never the sentence
"Device App isn't running". Worse, the iframe proxy returned that as JSON and the
browser rendered it AS the app's UI, so the owner's first sight of a stopped demo
app was a wall of urllib3 internals.

These tests pin the distinction that fixes it: `connection refused` (nothing
listening — start it) is a different `error_code` from a timeout (running but
silent), a bad address, or any other fault. The `<cid>_down` spelling is load
bearing: frontend/src/lib/fixes.ts resolves the Operations fix-it link from the
`_down` PATTERN, so a connector gets that link with no frontend change.
"""
import socket
import unittest

from ava_bridge import connectors


def _refused() -> Exception:
    """A real requests ConnectionError, with the real 3-deep wrapper chain.

    Built by connecting to a closed port rather than hand-assembling the chain —
    the point of the classifier is that it survives how requests ACTUALLY nests
    these, and a hand-built fake would happily pass a broken implementation.
    """
    import requests
    with socket.socket() as s:  # bind :0 and close it -> a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    try:
        requests.get(f"http://127.0.0.1:{port}/", timeout=3)
    except Exception as e:  # ruff.toml exempts tests/* from BLE001
        return e
    raise AssertionError("expected the connection to be refused")


class ClassifyTests(unittest.TestCase):
    def test_refused_is_down_not_a_generic_failure(self):
        info = connectors.unreachable("device-app", _refused(),
                                      url="http://127.0.0.1:8481")
        self.assertEqual(info["error_code"], "device-app_down")
        self.assertIn("isn't running", info["error"])

    def test_down_code_matches_the_frontend_fix_pattern(self):
        # fixes.ts keys the Operations link off `^(.+)_down$`. If this spelling
        # drifts the link silently disappears, with nothing else failing.
        info = connectors.unreachable("device-app", _refused())
        self.assertTrue(info["error_code"].endswith("_down"))

    def test_message_names_the_app_and_the_address(self):
        info = connectors.unreachable("device-app", _refused(),
                                      url="http://127.0.0.1:8481")
        self.assertIn("8481", info["error"])
        # The label from the manifest when there is one, else the id — never a
        # bare "connector 3" or the raw exception class.
        self.assertRegex(info["error"].lower(), r"device-app")

    def test_raw_exception_is_kept_as_detail_not_discarded(self):
        # The friendly sentence replaces the traceback in the headline, but a
        # non-obvious failure still needs the traceback to be fixable.
        info = connectors.unreachable("device-app", _refused())
        self.assertIn("detail", info)
        self.assertIn("Errno 111", info["detail"])
        self.assertNotIn("HTTPConnectionPool", info["error"])

    def test_timeout_is_distinct_from_down(self):
        import requests
        info = connectors.unreachable("home-assistant", requests.exceptions.ReadTimeout("slow"),
                                      url="http://127.0.0.1:8482")
        self.assertEqual(info["error_code"], "home-assistant_timeout")
        # "running, but not responding" — the opposite diagnosis from _down.
        self.assertNotIn("isn't running", info["error"])

    def test_dns_failure_points_at_the_manifest_not_at_start_the_app(self):
        import requests
        e = requests.exceptions.ConnectionError("boom")
        e.__cause__ = socket.gaierror(-2, "Name or service not known")
        info = connectors.unreachable("home-assistant", e, url="http://nope.invalid:8482")
        self.assertEqual(info["error_code"], "home-assistant_unreachable")
        self.assertIn("manifest", info["error"])

    def test_unknown_failure_still_classifies_rather_than_raising(self):
        info = connectors.unreachable("device-app", ValueError("weird"))
        self.assertEqual(info["error_code"], "device-app_error")
        self.assertTrue(info["error"])

    def test_cyclic_exception_chain_terminates(self):
        # __context__ chains can loop; the walker must not hang the bridge.
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__context__ = b
        b.__context__ = a
        info = connectors.unreachable("device-app", a)
        self.assertEqual(info["error_code"], "device-app_error")


class IframeRenderingTests(unittest.TestCase):
    """The iframe proxy must answer a NAVIGATION with HTML, not JSON.

    This is the half that made the failure ugly rather than merely unhelpful. The
    browser renders the proxy's response body as the frame document, so a JSON
    502 is displayed to the owner as literal JSON where the app's UI should be —
    and because a document DID arrive, the iframe fired `load` and AppFrame's
    12-second "isn't responding" state never ran.

    The frontend cannot repair this from outside: with `apps.origin` configured
    the frame is deliberately cross-origin, so React can neither read its body nor
    tell an error page from the app's own UI.
    """

    def _request(self, headers=None, query=""):
        from starlette.requests import Request
        raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        return Request({"type": "http", "method": "GET", "path": "/apps/device-app/",
                        "query_string": query.encode(), "headers": raw})

    def test_iframe_navigation_wants_a_document(self):
        import phone_bridge as pb
        self.assertTrue(pb._wants_document(self._request({"sec-fetch-dest": "iframe"})))
        self.assertTrue(pb._wants_document(self._request({"sec-fetch-dest": "document"})))

    def test_xhr_from_inside_the_app_still_gets_json(self):
        # An app's own fetch() must not be handed HTML to parse — that trades a
        # confusing screen for a confusing exception inside the app.
        import phone_bridge as pb
        self.assertFalse(pb._wants_document(self._request({"sec-fetch-dest": "empty"})))

    def test_falls_back_to_accept_when_sec_fetch_dest_is_absent(self):
        import phone_bridge as pb
        self.assertTrue(pb._wants_document(self._request({"accept": "text/html,*/*"})))
        self.assertFalse(pb._wants_document(self._request({"accept": "application/json"})))

    def test_page_states_the_problem_and_hides_the_traceback(self):
        import phone_bridge as pb
        info = connectors.unreachable("device-app", _refused(), url="http://127.0.0.1:8481")
        html = pb._unreachable_page(info, "Device App", "dark")
        self.assertIn("<!doctype html>", html)
        self.assertIn("isn&#x27;t running", html)          # the headline sentence
        self.assertIn("Technical detail", html)            # trace kept, collapsed
        self.assertIn("Errno 111", html)

    def test_page_escapes_its_inputs(self):
        # `detail` is exception text and `label` comes from a manifest on disk;
        # neither is trusted to be HTML-safe.
        import phone_bridge as pb
        html = pb._unreachable_page({"error": "<script>alert(1)</script>",
                                     "detail": "<img onerror=x>"}, "<b>App</b>", "light")
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img onerror=x>", html)
        self.assertNotIn("<b>App</b>", html)

    def test_page_honours_the_theme_the_frame_was_loaded_with(self):
        import phone_bridge as pb
        info = {"error": "x", "detail": ""}
        self.assertIn("#16181d", pb._unreachable_page(info, "A", "dark"))
        self.assertIn("#fbfbfc", pb._unreachable_page(info, "A", "light"))


class ErrnoWalkTests(unittest.TestCase):
    def test_walker_skips_the_requests_wrapper_and_finds_the_errno(self):
        # requests' exceptions subclass OSError, so a naive isinstance(e, OSError)
        # matches the OUTER wrapper and reports the wrong thing. This is the guard
        # for that specific mistake.
        root = connectors._errno_cause(_refused())
        self.assertIsInstance(root, ConnectionRefusedError)


if __name__ == "__main__":
    unittest.main()
