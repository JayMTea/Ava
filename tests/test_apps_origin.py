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
    def test_the_query_token_authorises_and_is_handed_back_for_a_cookie(self):
        t = apps_origin.mint("crm")
        ok, to_set, _ = apps_origin.authorize(_Req("apps.ava.test", {"t": t}),
                                              "/apps/crm/")
        self.assertTrue(ok)
        self.assertEqual(to_set, t)

    def test_an_asset_request_authorises_from_the_cookie_with_no_query(self):
        """Assets the app requests relative to its own document carry no query, so
        the one-time exchange to a cookie is what keeps them working."""
        t = apps_origin.mint("crm")
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


if __name__ == "__main__":
    unittest.main()
