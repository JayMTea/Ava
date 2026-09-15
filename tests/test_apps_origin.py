"""Embedded connector apps must not run on Ava's origin.

`AppFrame` sandboxes the iframe `allow-scripts allow-forms allow-same-origin`, and
the bridge reverse-proxies the app under `/apps/<cid>/` on Ava's OWN origin. That
pairing keeps Ava's origin, Ava serves no CSP, and `/api/hub/*` has no CSRF or
Origin check — the session cookie is the only gate and a same-origin frame sends it.
So an embedded app could run:

    fetch('/api/hub/approvals').then(r => r.json())
      .then(j => j.pending.forEach(p => fetch('/api/hub/approvals/' + p.id,
                                              {method: 'POST'})))

and approve Ava's consent prompts on the owner's behalf.

An Origin check cannot close it — the frame is *genuinely* same-origin, so every
request header is identical to the real SPA's. The fix is a second origin, and the
two rules below are symmetric on purpose: serving `/apps/*` only on the apps host
moves the app off Ava's origin, and refusing everything else on that host is what
makes the move mean anything. Either alone is not enough.

Unit-level: `refuses()` / `authorize()` / `verify()` are pure given a request-shaped
object, so no bridge, no browser and no second hostname are needed here. What is NOT
covered is stated in the commit — nothing has been loaded in a real browser.
"""
import time
import unittest
from unittest import mock

from ava_bridge import apps_origin

ORIGIN = "http://apps.ava.test:8096"


class _Req:
    """The three things apps_origin reads off a request."""

    def __init__(self, host, query=None, cookies=None, client="1.2.3.4",
                 fwd_host=None, scheme="http"):
        self.headers = {"host": host}
        if fwd_host:
            self.headers["x-forwarded-host"] = fwd_host
        self.query_params = query or {}
        self.cookies = cookies or {}
        self.client = type("C", (), {"host": client})()
        self.url = type("U", (), {"scheme": scheme})()


def _with_origin(value=ORIGIN):
    return mock.patch.object(apps_origin.settings, "get",
                             side_effect=lambda d, default=None, env=None:
                             value if d == "apps.origin" else default)


class ConfigurationTests(unittest.TestCase):
    def test_unset_means_disabled(self):
        with _with_origin(""):
            self.assertIsNone(apps_origin.configured())
            self.assertIsNone(apps_origin.refuses(_Req("ava.test"), "/apps/x/"))
            self.assertIsNone(apps_origin.embed_url("x"))

    def test_a_garbage_value_is_treated_as_unset_not_as_a_host(self):
        for bad in ("apps.ava.test", "://nope", "   "):
            with _with_origin(bad):
                self.assertIsNone(apps_origin.configured(), bad)

    def test_it_normalises_away_a_trailing_slash(self):
        with _with_origin(ORIGIN + "/"):
            self.assertEqual(apps_origin.configured(), ORIGIN)

    def test_the_default_is_reported_as_not_ok(self):
        """Setup must say the unconfigured state is the unsafe one."""
        with _with_origin(""):
            w = apps_origin.warning()
        self.assertFalse(w["ok"])
        self.assertIn("consent", w["detail"])

    def test_configured_reports_ok_with_the_origin(self):
        with _with_origin():
            self.assertEqual(apps_origin.warning(),
                             {"ok": True, "origin": ORIGIN})


class OriginSplitTests(unittest.TestCase):
    """Both directions. Either rule alone leaves the hole open."""

    def test_apps_paths_are_refused_on_avas_own_host(self):
        with _with_origin():
            why = apps_origin.refuses(_Req("ava.test:8096"), "/apps/crm/")
        self.assertIsNotNone(why)

    def test_apps_paths_are_allowed_on_the_apps_host(self):
        with _with_origin():
            self.assertIsNone(apps_origin.refuses(_Req("apps.ava.test:8096"),
                                                  "/apps/crm/"))

    def test_the_api_is_refused_on_the_apps_host(self):
        """THE property. Without this the app just calls /api/hub on its own origin."""
        with _with_origin():
            for p in ("/api/hub/approvals", "/api/apps", "/api/chat-stream",
                      "/media/x.png", "/", "/internal/connector/crm/__call"):
                self.assertIsNotNone(
                    apps_origin.refuses(_Req("apps.ava.test:8096"), p), p)

    def test_the_api_is_allowed_on_avas_own_host(self):
        with _with_origin():
            for p in ("/api/hub/approvals", "/api/apps", "/"):
                self.assertIsNone(apps_origin.refuses(_Req("ava.test:8096"), p), p)

    def test_a_default_port_origin_matches_a_hostless_port(self):
        with _with_origin("https://apps.ava.test"):
            self.assertTrue(apps_origin.on_apps_host(_Req("apps.ava.test:443")))
            self.assertTrue(apps_origin.on_apps_host(_Req("apps.ava.test")))

    def test_host_matching_is_case_insensitive(self):
        with _with_origin():
            self.assertTrue(apps_origin.on_apps_host(_Req("APPS.AVA.TEST:8096")))

    def test_a_forwarded_host_is_believed_only_from_a_trusted_proxy(self):
        """Same restriction serve.py puts on proxy_headers, same reason: accepting
        it from any peer lets a caller claim whichever origin gets it through."""
        with _with_origin(), \
                mock.patch.object(apps_origin.config, "TRUSTED_PROXIES", ("127.0.0.1",)):
            spoof = _Req("ava.test:8096", client="9.9.9.9",
                         fwd_host="apps.ava.test:8096")
            self.assertFalse(apps_origin.on_apps_host(spoof))
            trusted = _Req("ava.test:8096", client="127.0.0.1",
                           fwd_host="apps.ava.test:8096")
            self.assertTrue(apps_origin.on_apps_host(trusted))


class EmbedTokenTests(unittest.TestCase):
    def test_a_minted_token_verifies_for_its_own_cid_only(self):
        t = apps_origin.mint("crm")
        self.assertTrue(apps_origin.verify("crm", t))
        self.assertFalse(apps_origin.verify("other", t),
                         "a token for one app must not load another's proxy")

    def test_an_expired_token_is_refused(self):
        self.assertFalse(apps_origin.verify("crm", apps_origin.mint("crm", ttl_s=-1)))

    def test_a_tampered_signature_is_refused(self):
        exp = int(time.time()) + 300
        self.assertFalse(apps_origin.verify("crm", f"{exp}.{'0' * 32}"))

    def test_a_tampered_expiry_is_refused(self):
        t = apps_origin.mint("crm")
        _, sig = t.split(".", 1)
        self.assertFalse(apps_origin.verify("crm", f"{int(time.time()) + 99999}.{sig}"))

    def test_malformed_tokens_do_not_raise(self):
        for bad in ("", None, "nodot", "x.y", "..", "abc.def"):
            self.assertFalse(apps_origin.verify("crm", bad), repr(bad))

    def test_the_embed_url_is_absolute_and_carries_the_token(self):
        with _with_origin():
            url = apps_origin.embed_url("crm", "theme=dark&embedded=1")
        self.assertTrue(url.startswith(ORIGIN + "/apps/crm/?"))
        self.assertIn("theme=dark", url)
        tok = url.split("t=", 1)[1]
        self.assertTrue(apps_origin.verify("crm", tok))


class AuthorizeTests(unittest.TestCase):
    def test_the_query_token_authorises_and_is_exchanged_for_a_cookie_token(self):
        t = apps_origin.mint("crm")
        ok, to_set, _ = apps_origin.authorize(_Req("apps.ava.test", {"t": t}),
                                              "/apps/crm/")
        self.assertTrue(ok)
        self.assertTrue(apps_origin.verify("crm", to_set))
        # The URL token's job ends at the exchange: what the cookie holds is minted
        # for the cookie's own, longer life — never the five-minute URL token itself.
        self.assertNotEqual(to_set, t)
        self.assertGreaterEqual(int(to_set.split(".", 1)[0]),
                                int(time.time()) + apps_origin.COOKIE_TTL_S - 5)

    def test_an_asset_request_authorises_from_the_cookie_with_no_query(self):
        """Assets the app requests relative to its own document carry no query, so
        the one-time exchange to a cookie is what keeps them working."""
        # What the cookie actually holds after the exchange: a cookie-life token.
        # A five-minute one would be an aging cookie now, and renewed on sight.
        t = apps_origin.mint("crm", ttl_s=apps_origin.COOKIE_TTL_S)
        ok, to_set, _ = apps_origin.authorize(
            _Req("apps.ava.test", cookies={apps_origin.cookie_name("crm"): t}),
            "/apps/crm/assets/main.js")
        self.assertTrue(ok)
        self.assertIsNone(to_set, "nothing to re-set when it came from the cookie")

    def test_no_token_at_all_is_refused(self):
        ok, _, reason = apps_origin.authorize(_Req("apps.ava.test"), "/apps/crm/")
        self.assertFalse(ok)
        self.assertIn("no embed token", reason)

    def test_another_apps_cookie_does_not_authorise_this_one(self):
        t = apps_origin.mint("other")
        ok, _, _ = apps_origin.authorize(
            _Req("apps.ava.test", cookies={apps_origin.cookie_name("crm"): t}),
            "/apps/crm/")
        self.assertFalse(ok)

    def test_an_expired_token_says_so_rather_than_looking_absent(self):
        ok, _, reason = apps_origin.authorize(
            _Req("apps.ava.test", {"t": apps_origin.mint("crm", ttl_s=-1)}),
            "/apps/crm/")
        self.assertFalse(ok)
        self.assertIn("expired", reason)

    def test_a_pathless_apps_request_is_refused(self):
        ok, _, _ = apps_origin.authorize(_Req("apps.ava.test"), "/apps")
        self.assertFalse(ok)

    def test_cid_extraction(self):
        self.assertEqual(apps_origin.cid_from_path("/apps/crm/a/b"), "crm")
        self.assertEqual(apps_origin.cid_from_path("/apps/crm/"), "crm")
        self.assertEqual(apps_origin.cid_from_path("/apps"), "")


class CookieScopeTests(unittest.TestCase):
    def test_the_cookie_is_httponly_lax_and_scoped_to_one_app(self):
        seen = {}

        class R:
            def set_cookie(self, k, v, **kw):
                seen.update({"k": k, "v": v, **kw})

        apps_origin.apply_cookie(R(), "crm", "tok", secure=True)
        self.assertEqual(seen["k"], "ava_app_crm")
        self.assertTrue(seen["httponly"], "the app's own JS must not read it")
        self.assertEqual(seen["samesite"], "lax")
        self.assertTrue(seen["secure"])
        self.assertEqual(seen["path"], "/apps/crm/",
                         "one app's cookie must not be offered to another's proxy")


class TopLevelVisitBouncesToTheShellTests(unittest.TestCase):
    """A bookmarked app URL must land in Ava, and only a bookmark may.

    `phone_bridge.app_ui_proxy` redirects a typed `/apps/<id>/` to `/#<id>` so
    nobody is stranded in the bare app. With `apps.origin` set the origin split
    answers first and that line never runs — measured as a raw
    `{"error":"forbidden","detail":"no embed token"}` page on the apps host and a
    `{"error":"wrong origin"}` on the main one.

    The bounce is gated on `Sec-Fetch-Dest: document`, which is the whole safety
    argument: the iframe load itself sends `iframe` and app JS sends `empty`, and
    both must keep getting the 403 — that refusal IS the boundary this module
    exists to draw. These four cases are the negative controls.
    """

    def _gate(self, dest, method="GET", path="/apps/crm/", public="http://ava.test:8096"):
        from unittest import mock as _mock

        from ava_bridge import auth
        req = _mock.Mock()
        req.method = method
        req.headers = {"sec-fetch-dest": dest} if dest else {}
        with _mock.patch.object(apps_origin, "configured", return_value=ORIGIN), \
             _mock.patch.object(auth.config, "PUBLIC_URL", public):
            return auth._shell_bounce(req, path)

    def test_a_typed_url_is_bounced_into_the_shell(self):
        r = self._gate("document")
        self.assertIsNotNone(r, "a top-level visit must not get raw JSON")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "http://ava.test:8096/#crm")

    def test_the_iframe_load_itself_is_never_bounced(self):
        self.assertIsNone(self._gate("iframe"),
                          "bouncing the frame would replace the app with Ava")

    def test_app_javascript_is_never_bounced(self):
        self.assertIsNone(self._gate("empty"),
                          "a 302 to the shell would read as success to fetch()")

    def test_a_non_navigation_method_is_never_bounced(self):
        self.assertIsNone(self._gate("document", method="POST"))

    def test_a_path_that_is_not_a_clean_cid_is_never_bounced(self):
        for bad in ("/apps/../etc/passwd", "/apps", "/apps//", "/api/hub/x"):
            self.assertIsNone(self._gate("document", path=bad), bad)

    def test_the_target_comes_from_config_not_the_request(self):
        """No request field reaches the Location header, so no open redirect."""
        r = self._gate("document", public="https://elsewhere.example")
        self.assertEqual(r.headers["location"], "https://elsewhere.example/#crm")

    def test_nothing_is_bounced_when_the_split_is_off(self):
        from unittest import mock as _mock

        from ava_bridge import auth
        req = _mock.Mock()
        req.method = "GET"
        req.headers = {"sec-fetch-dest": "document"}
        with _mock.patch.object(apps_origin, "configured", return_value=None):
            self.assertIsNone(auth._shell_bounce(req, "/apps/crm/"),
                              "single-origin installs already redirect in "
                              "phone_bridge.app_ui_proxy; two would fight")


class TwoLifetimesTests(unittest.TestCase):
    """The URL token and the cookie it becomes do not share a clock.

    The cookie used to inherit the URL token's five minutes and was renewed only
    by traffic. A phone freezes a backgrounded page, so nothing renewed it, and
    the first tap after unlocking got the refusal as the app's whole UI. The URL
    token keeps its five minutes (it leaks: history, screenshots, Referer); the
    cookie — HttpOnly, path-scoped, never in a URL — gets a working day.
    """

    def test_the_cookie_outlives_the_url_token_by_design(self):
        self.assertGreater(apps_origin.COOKIE_TTL_S, apps_origin.TOKEN_TTL_S)
        self.assertEqual(apps_origin.TOKEN_TTL_S, 300,
                         "the URL token stays short — a copied one must go stale")

    def test_renewal_is_measured_against_the_cookie_life(self):
        aging = apps_origin.mint("crm", ttl_s=100)
        fresh = apps_origin.mint("crm", ttl_s=apps_origin.COOKIE_TTL_S)
        renewed = apps_origin._renewal("crm", aging)
        self.assertIsNotNone(renewed)
        self.assertTrue(apps_origin.verify("crm", renewed))
        self.assertGreaterEqual(int(renewed.split(".", 1)[0]),
                                int(time.time()) + apps_origin.COOKIE_TTL_S - 5,
                                "a renewal is a full cookie life, not another five minutes")
        self.assertIsNone(apps_origin._renewal("crm", fresh),
                          "a cookie in its first half-life is not churned")

    def test_the_cookie_is_set_for_its_own_life(self):
        seen = {}

        class R:
            def set_cookie(self, k, v, **kw):
                seen.update({"k": k, "v": v, **kw})

        apps_origin.apply_cookie(R(), "crm", "tok", secure=True)
        self.assertEqual(seen["max_age"], apps_origin.COOKIE_TTL_S)


class _FrameReq:
    """What `wants_frame_document`, `resume_path` and `_reconnect_page` read."""

    def __init__(self, dest="iframe", method="GET", path="/apps/crm/", query=None,
                 accept=None):
        self.method = method
        self.headers = {}
        if dest is not None:
            self.headers["sec-fetch-dest"] = dest
        if accept is not None:
            self.headers["accept"] = accept
        self.query_params = query or {}
        self.url = type("U", (), {"path": path, "scheme": "https"})()
        self.cookies = {}


class ReconnectPageTests(unittest.TestCase):
    """A dead FRAME gets a page that asks the shell for a fresh token.

    Between `_shell_bounce` (top-level: redirect) and the raw 403 (app JS: JSON)
    sits the frame itself navigating on a token the bridge no longer accepts —
    the case where JSON was rendered verbatim as the app's whole UI, on a phone
    with no reload button. The refusal is unchanged (still 403, nothing served);
    only the body knows how to recover, and only to the configured shell.
    """

    def test_only_framed_navigations_want_the_page(self):
        for dest in ("iframe", "frame", "embed", "object"):
            self.assertTrue(apps_origin.wants_frame_document(_FrameReq(dest)), dest)
        self.assertFalse(apps_origin.wants_frame_document(_FrameReq("document")),
                         "a top-level visit belongs to _shell_bounce")
        self.assertFalse(apps_origin.wants_frame_document(_FrameReq("empty")),
                         "app JS must keep getting JSON it can parse")
        self.assertFalse(apps_origin.wants_frame_document(_FrameReq("iframe", method="POST")))
        self.assertTrue(apps_origin.wants_frame_document(
            _FrameReq(None, accept="text/html,application/xhtml+xml")),
            "no Fetch Metadata at all: the Accept header decides")
        self.assertFalse(apps_origin.wants_frame_document(
            _FrameReq(None, accept="application/json")))

    def test_the_resume_path_is_relative_to_the_app_and_drops_the_spent_token(self):
        req = _FrameReq(path="/apps/crm/reports/7", query={"x": "1", "t": "1.dead"})
        self.assertEqual(apps_origin.resume_path(req, "crm"), "/reports/7?x=1")
        self.assertEqual(apps_origin.resume_path(_FrameReq(path="/apps/crm/"), "crm"), "/")
        self.assertEqual(apps_origin.resume_path(_FrameReq(path="/apps/crm"), "crm"), "/")
        self.assertEqual(apps_origin.resume_path(_FrameReq(path="/elsewhere"), "crm"), "/")

    def test_the_page_reports_to_the_configured_shell_and_nowhere_else(self):
        with mock.patch.object(apps_origin.config, "PUBLIC_URL", "http://ava.test:8096/"):
            html = apps_origin.reconnect_page("crm", "CRM", "embed token expired",
                                              "dark", "/reports/7?x=1")
        self.assertIn('postMessage(msg,shell)', html)
        self.assertIn('shell="http://ava.test:8096"', html)
        self.assertIn('"type": "ava:embed-expired", "cid": "crm", "path": "/reports/7?x=1"',
                      html)
        # No parent to ask: send itself to the shell, as _shell_bounce would have.
        self.assertIn('tile="http://ava.test:8096/#crm"', html)
        self.assertIn("window.location.replace(tile)", html)
        self.assertIn("Reconnecting CRM", html)

    def test_nothing_from_the_request_can_break_out_of_the_page(self):
        with mock.patch.object(apps_origin.config, "PUBLIC_URL", "http://ava.test:8096"):
            html = apps_origin.reconnect_page(
                "crm", "<b>x</b>", "</pre><script>alert(1)</script>", "light",
                "/</script><script>alert(2)</script>")
        self.assertNotIn("<b>x</b>", html)
        self.assertNotIn("</pre><script>", html)
        self.assertNotIn("</script><script>alert(2)", html,
                         "a path may not end the <script> it is quoted inside")
        self.assertIn("<\\/script>", html)

    def _gate(self, req, path="/apps/crm/", configured=ORIGIN):
        from ava_bridge import auth
        with mock.patch.object(apps_origin, "configured", return_value=configured), \
             mock.patch.object(auth.config, "PUBLIC_URL", "http://ava.test:8096"):
            return auth._reconnect_page(req, path, "embed token expired — reload the app in Ava")

    def test_a_dead_frame_gets_the_page_as_a_403(self):
        r = self._gate(_FrameReq("iframe", path="/apps/crm/reports/7",
                                 query={"theme": "light", "t": "1.dead"}),
                       path="/apps/crm/reports/7")
        self.assertIsNotNone(r, "a framed navigation must not get raw JSON")
        self.assertEqual(r.status_code, 403, "the refusal is unchanged; only its body is")
        self.assertIn("text/html", r.headers["content-type"])
        self.assertEqual(r.headers["cache-control"], "no-store")
        body = r.body.decode()
        self.assertIn('"path": "/reports/7?theme=light"', body)
        self.assertIn("embed token expired", body)

    def test_app_javascript_and_top_level_visits_are_not_given_the_page(self):
        self.assertIsNone(self._gate(_FrameReq("empty")),
                          "fetch() must keep getting JSON")
        self.assertIsNone(self._gate(_FrameReq("document")),
                          "a top-level visit is _shell_bounce's, which runs first")
        self.assertIsNone(self._gate(_FrameReq("iframe", method="POST")))

    def test_the_page_is_not_served_off_the_split_or_for_a_dirty_cid(self):
        self.assertIsNone(self._gate(_FrameReq("iframe"), configured=None))
        for bad in ("/apps/../etc/passwd", "/apps", "/apps//"):
            self.assertIsNone(self._gate(_FrameReq("iframe", path=bad), path=bad), bad)


if __name__ == "__main__":
    unittest.main()
