"""Where route memory may be added to an app's document, and where it may not.

`ava_bridge/embed_route.py` decides three things for every framed document the
proxy carries: whether this response is one we may edit at all, where in the
bytes the tag goes, and whether the app's own Content-Security-Policy leaves
room for it. All three are pure functions over bytes and header pairs, so they
are pinned here without a bridge, a browser or a socket — the proxy end of the
same feature is pinned from the outside in
tests/test_app_proxy_contract.py::RouteInjectionTests.

Each case below is a way of NOT corrupting somebody's app:
  * a tag before the doctype puts the whole page in quirks mode;
  * a tag ahead of `<meta charset>` can push the declaration out of the first
    1024 bytes the encoding sniffer reads, changing how the app's own text
    decodes;
  * a `<head` written inside a comment is not a head;
  * a policy we cannot satisfy means injecting nothing, never widening the
    policy;
  * and a document that streams must keep streaming.

House style: stdlib unittest, no network, no AVA_HOME.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ava_bridge import config, connectors, embed_route

SHELL = "https://ava.example"
CID = "demo"


def _point(html: bytes, **kw):
    return embed_route.injection_point(html, **kw)


def _stream(chunks, tag: bytes, **kw):
    """`chunks` with `tag` spliced in once, driven through the real `Injector`.

    The drain lives here rather than in embed_route because the ONE production
    caller is async (`phone_bridge._inject_stream`): a synchronous twin beside
    the class would be a second copy of the loop, and the tested copy would not
    be the code that runs.
    """
    injector = embed_route.Injector(tag, **kw)
    out = [injector.feed(chunk) for chunk in chunks]
    return b"".join(out) + injector.finish()


class InsertionPointTests(unittest.TestCase):
    """Where the tag goes, and when the scanner refuses to say."""

    def test_the_doctype_is_never_jumped(self):
        html = b'<!DOCTYPE html><html><head><title>x</title></head>'
        at = _point(html)
        self.assertGreater(at, html.index(b">"),
                           "a tag ahead of the doctype puts the page in quirks "
                           "mode — the app's layout, not just ours")
        self.assertEqual(html[:at], b'<!DOCTYPE html><html><head>')

    def test_a_leading_meta_charset_keeps_its_place(self):
        html = b'<!doctype html><html><head><meta charset="utf-8"><title>x</title>'
        at = _point(html)
        self.assertEqual(html[:at],
                         b'<!doctype html><html><head><meta charset="utf-8">',
                         "the charset declaration has to stay inside the first "
                         "1024 bytes the sniffer reads")

    def test_a_content_type_meta_counts_as_the_charset_declaration(self):
        html = (b'<html><head>'
                b'<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
                b'<link rel="x">')
        at = _point(html)
        self.assertTrue(html[:at].endswith(b'charset=utf-8">'))

    def test_a_quoted_angle_bracket_does_not_cut_a_tag_in_half(self):
        # `content=">"` is legal, and a plain find(b'>') would end the meta
        # early and leave half a tag in front of our script.
        html = b'<html><head><meta name="x" content="a>b"><title>t</title>'
        at = _point(html)
        self.assertEqual(html[:at], b'<html><head>',
                         "a non-charset meta is not a tag to get in behind")

    def test_a_head_inside_a_comment_is_not_a_head(self):
        html = b'<!doctype html><!-- <head> not really --><html><head><title>x'
        at = _point(html)
        self.assertEqual(html[:at],
                         b'<!doctype html><!-- <head> not really --><html><head>')

    def test_no_head_means_just_before_the_body(self):
        html = b'<!doctype html><body><p>hi</p>'
        self.assertEqual(html[:_point(html)], b'<!doctype html>')

    def test_no_head_and_no_body_still_lands_after_the_html_tag(self):
        html = b'<!doctype html><html><p>hi</p>'
        self.assertEqual(html[:_point(html)], b'<!doctype html><html>')

    def test_a_bare_fragment_with_neither_lands_at_the_top(self):
        self.assertEqual(_point(b'<p>hi</p>'), 0)

    def test_a_base_tag_is_never_jumped(self):
        html = b'<html><head><base href="/x/"><meta charset="utf-8">'
        self.assertEqual(html[:_point(html)], b'<html><head>',
                         "a <base> changes what every relative URL after it "
                         "means; get in ahead of it or not at all")

    def test_a_meta_csp_is_never_jumped(self):
        html = (b'<html><head>'
                b'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'">'
                b'<title>x')
        self.assertEqual(html[:_point(html)], b'<html><head>')

    def test_a_utf8_bom_stays_the_first_bytes(self):
        html = b'\xef\xbb\xbf<!doctype html><html><head><title>x'
        self.assertEqual(html[:_point(html)],
                         b'\xef\xbb\xbf<!doctype html><html><head>')

    def test_a_utf16_document_is_left_alone(self):
        self.assertIsNone(_point(b'\xff\xfe<\x00h\x00t\x00m\x00l\x00>\x00'))
        self.assertIsNone(_point(b'\xfe\xff\x00<\x00h'))

    def test_a_tag_split_across_chunks_waits_for_the_rest(self):
        self.assertEqual(_point(b'<!doctype html><html><head><meta char'),
                         embed_route.PENDING)
        self.assertEqual(_point(b'<!doctype html><!-- unclosed'),
                         embed_route.PENDING)
        self.assertEqual(_point(b'<!doctype html><html><head>'),
                         embed_route.PENDING,
                         "what follows <head> decides whether a charset meta "
                         "comes first — that needs the next bytes")

    def test_a_body_that_never_resolves_is_passed_through(self):
        filler = b"<!--" + b"x" * (embed_route.SCAN_LIMIT + 10) + b"-->"
        self.assertIsNone(_point(filler),
                          "past the bounded scan, guessing is worse than "
                          "leaving the app exactly as it was")

    def test_the_end_of_the_document_settles_a_pending_scan(self):
        html = b'<!doctype html><html><head>'
        self.assertEqual(_point(html, complete=True), len(html))
        self.assertEqual(_point(b'<!doctype html>', complete=True), 15)


def _csp(*values):
    return [("Content-Security-Policy", v) for v in values]


class CspTests(unittest.TestCase):
    """An app's policy is read, never edited. What it leaves room for decides
    which tag (if any) the bridge is allowed to add."""

    def test_no_policy_means_a_plain_inline_tag(self):
        self.assertEqual(embed_route.csp_decision([]), ("inline", None))

    def test_unsafe_inline_means_a_plain_inline_tag(self):
        self.assertEqual(
            embed_route.csp_decision(_csp("script-src 'self' 'unsafe-inline'")),
            ("inline", None))

    def test_a_report_only_policy_blocks_nothing(self):
        self.assertEqual(
            embed_route.csp_decision(
                [("content-security-policy-report-only", "script-src 'none'")]),
            ("inline", None),
            "Report-Only reports; treating it as a refusal would switch route "
            "memory off for an app merely measuring a policy")

    def test_a_nonce_is_adopted_rather_than_the_policy_widened(self):
        kind, nonce = embed_route.csp_decision(
            _csp("script-src 'nonce-r4nd0mBASE64==' 'unsafe-inline'"))
        self.assertEqual((kind, nonce), ("inline", "r4nd0mBASE64=="),
                         "CSP3 makes 'unsafe-inline' a no-op once a nonce is "
                         "present, so the nonce is the only way in")

    def test_a_malformed_nonce_is_not_treated_as_permission(self):
        # The browser still ignores 'unsafe-inline' because a nonce source is
        # present; reading it as permission would inline a script that is dropped.
        self.assertEqual(
            embed_route.csp_decision(_csp("script-src 'nonce-x' 'unsafe-inline' 'self'")),
            ("external", None))

    def test_script_src_elem_wins_over_script_src_and_default_src(self):
        self.assertEqual(
            embed_route.csp_decision(_csp(
                "default-src 'none'; script-src 'none'; "
                "script-src-elem 'unsafe-inline'")),
            ("inline", None))
        self.assertEqual(
            embed_route.csp_decision(_csp("default-src 'none'; script-src 'self'")),
            ("external", None))

    def test_default_src_governs_when_nothing_else_does(self):
        self.assertEqual(embed_route.csp_decision(_csp("default-src 'self'")),
                         ("external", None))

    def test_a_policy_that_says_nothing_about_scripts_restricts_nothing(self):
        self.assertEqual(embed_route.csp_decision(_csp("img-src 'self'")),
                         ("inline", None))

    def test_self_without_inline_takes_the_external_tag(self):
        self.assertEqual(embed_route.csp_decision(_csp("script-src 'self'")),
                         ("external", None))

    def test_strict_dynamic_with_a_nonce_still_takes_the_nonce(self):
        self.assertEqual(
            embed_route.csp_decision(
                _csp("script-src 'strict-dynamic' 'nonce-abcd1234' 'self'")),
            ("inline", "abcd1234"))

    def test_strict_dynamic_without_a_nonce_blocks_everything(self):
        self.assertEqual(
            embed_route.csp_decision(_csp("script-src 'strict-dynamic' 'self'")),
            ("blocked", None),
            "'strict-dynamic' makes 'self' ignored, so the external tag is not "
            "a way in either")

    def test_none_blocks_everything(self):
        self.assertEqual(embed_route.csp_decision(_csp("script-src 'none'")),
                         ("blocked", None))

    def test_a_sandbox_policy_blocks_everything(self):
        self.assertEqual(
            embed_route.csp_decision(_csp("sandbox allow-scripts; script-src 'unsafe-inline'")),
            ("blocked", None),
            "a sandbox policy re-makes the document's origin; it is not a "
            "document to add code to")

    def test_two_enforcing_policies_must_both_admit_the_choice(self):
        self.assertEqual(
            embed_route.csp_decision(
                _csp("script-src 'unsafe-inline'", "script-src 'self'")),
            ("blocked", None),
            "one allows only inline, the other only same-origin files — there "
            "is no tag both would run")
        self.assertEqual(
            embed_route.csp_decision(
                _csp("script-src 'nonce-aaaabbbb'", "script-src 'nonce-aaaabbbb' 'self'")),
            ("inline", "aaaabbbb"))

    def test_policies_demanding_different_nonces_inject_nothing(self):
        self.assertEqual(
            embed_route.csp_decision(
                _csp("script-src 'nonce-aaaabbbb'", "script-src 'nonce-ccccdddd'")),
            ("blocked", None))

    def test_one_header_may_carry_several_policies(self):
        self.assertEqual(
            embed_route.csp_decision(
                _csp("script-src 'unsafe-inline', script-src 'none'")),
            ("blocked", None))

    def test_the_tag_carries_the_nonce_and_the_external_tag_is_root_relative(self):
        with mock.patch.object(config, "PUBLIC_URL", SHELL):
            nonced = embed_route.script_tag(
                CID, _csp("script-src 'nonce-abcd1234'"))
            external = embed_route.script_tag(CID, _csp("script-src 'self'"))
            blocked = embed_route.script_tag(CID, _csp("script-src 'none'"))
        self.assertIn(b'<script nonce="abcd1234">', nonced)
        self.assertEqual(
            external, b'<script src="/apps/demo/.ava/route.js"></script>',
            "an app may install a runtime <base href>, so the src has to be "
            "root-relative or it resolves against whatever the app chose")
        self.assertIsNone(blocked)

    def test_a_cid_that_is_not_a_connector_id_gets_no_tag(self):
        self.assertIsNone(embed_route.script_tag('a"><b', []))


class ShimSourceTests(unittest.TestCase):
    """The file that is spliced into somebody else's bytes."""

    def test_the_source_is_ascii_only(self):
        with open(embed_route.SHIM_PATH, "rb") as f:
            raw = f.read()
        raw.decode("ascii")   # raises if not — the assertion IS the decode
        self.assertIn(b"__CID__", raw)
        self.assertIn(b"__SHELL__", raw)

    def test_both_placeholders_are_substituted_as_whole_literals(self):
        with mock.patch.object(config, "PUBLIC_URL", SHELL + "/"):
            src = embed_route.shim(CID)
        self.assertNotIn("__CID__", src)
        self.assertNotIn("__SHELL__", src)
        self.assertIn('var CID = "demo";', src)
        self.assertIn('var SHELL = "https://ava.example";', src)

    def test_a_closing_script_tag_can_never_escape_the_element(self):
        with mock.patch("ava_bridge.apps_origin.shell_origin",
                        return_value="https://x/</script><b>"):
            src = embed_route.shim(CID)
        self.assertNotIn("</script>", src,
                         "a `</` inside the literal would end the <script> that "
                         "carries it, as far as the HTML parser is concerned")


class StreamingTests(unittest.TestCase):
    """Buffer until the point is known, then get out of the way."""

    TAG = b"<!--TAG-->"

    def test_later_chunks_are_passed_through_unbuffered(self):
        injector = embed_route.Injector(self.TAG)
        first = injector.feed(b'<!doctype html><html><head><title>x</title>')
        self.assertEqual(
            first, b'<!doctype html><html><head>' + self.TAG + b'<title>x</title>')
        for chunk in (b"<p>a</p>", b"<p>b</p>", b""):
            self.assertEqual(injector.feed(chunk), chunk,
                             "once the point is settled a chunk must come back "
                             "as it arrived — a streamed app that paints in "
                             "200ms must not wait for its last byte")
        self.assertEqual(injector.finish(), b"")

    def test_a_point_split_across_chunks_holds_back_only_until_it_resolves(self):
        injector = embed_route.Injector(self.TAG)
        self.assertEqual(injector.feed(b"<!doctype html><html><he"), b"")
        self.assertEqual(injector.feed(b"ad><meta char"), b"")
        out = injector.feed(b'set="utf-8"><title>x')
        self.assertEqual(
            out,
            b'<!doctype html><html><head><meta charset="utf-8">' + self.TAG
            + b'<title>x')

    def test_a_document_that_ends_undecided_still_gets_the_tag(self):
        self.assertEqual(_stream([b"<html><head>"], self.TAG),
                         b"<html><head>" + self.TAG)

    def test_a_body_past_the_scan_limit_comes_out_byte_identical(self):
        chunks = [b"<!--", b"x" * (embed_route.SCAN_LIMIT + 10), b"-->tail"]
        self.assertEqual(_stream(chunks, self.TAG), b"".join(chunks))

    def test_the_tag_is_spliced_exactly_once(self):
        body = b"<html><head></head><body><html><head></head></body></html>"
        self.assertEqual(_stream([body], self.TAG).count(self.TAG), 1)


class PrescanTests(unittest.TestCase):
    """A charset declaration the app's own text depends on is never pushed out
    of the window the browser reads it in.

    Only when the transport named no charset — a `Content-Type: text/html` with
    no parameter, which every small static server sends. A ~9 KB tag spliced in
    ahead of the declaration puts it past the first 1024 bytes, and the app
    renders as mojibake in Ava's frame and nowhere else.
    """

    TAG = b"<!--" + b"t" * 9000 + b"-->"
    LATE = (b'<!doctype html><html><head><title>My App</title>'
            b'<meta charset="utf-8"><link rel="x"></head><body>hi')

    def test_a_charset_meta_that_is_not_first_is_still_insured(self):
        out = _stream([self.LATE], self.TAG, prescan=True)
        self.assertLess(out.index(b'charset="utf-8"'), embed_route.PRESCAN,
                        "the declaration has to stay inside the prescan window "
                        "of the body we actually deliver")
        self.assertLess(out.index(b'charset="utf-8"'), out.index(self.TAG),
                        "insert AFTER it, rather than shoving it along")

    def test_the_transport_naming_a_charset_keeps_the_tag_first(self):
        out = _stream([self.LATE], self.TAG)
        self.assertLess(out.index(self.TAG), out.index(b"<title>"),
                        "a Content-Type charset outranks the document, so "
                        "first-in-head is free to win again")

    def test_a_leading_declaration_needs_no_lookahead(self):
        html = b'<!doctype html><html><head><meta charset="utf-8"><title>x'
        self.assertEqual(_point(html, prescan=True), _point(html))

    def test_a_head_with_no_declaration_is_injected_first_as_before(self):
        html = b'<!doctype html><html><head><title>x</title></head><body>hi'
        self.assertEqual(_point(html, prescan=True, complete=True),
                         _point(html, complete=True),
                         "nothing in the window to displace, so nothing to buy "
                         "by going later")

    def test_the_lookahead_waits_for_the_window_rather_than_guessing(self):
        self.assertEqual(_point(b'<!doctype html><html><head><title>x</title>',
                                prescan=True),
                         embed_route.PENDING,
                         "a declaration could be in the next bytes; inserting "
                         "before one we have not seen is exactly the bug")
        filler = b"<p>" + b"y" * embed_route.PRESCAN
        self.assertIsNotNone(
            _point(b'<!doctype html><html><head><title>x</title>' + filler,
                   prescan=True))

    def test_a_declaration_after_the_head_is_not_one_the_browser_read(self):
        html = (b'<!doctype html><html><head><title>x</title></head><body>'
                b'<meta charset="utf-8">')
        self.assertEqual(_point(html, prescan=True), _point(html),
                         "past </head> it was never going to decide the "
                         "encoding, so moving it costs nothing")


class GateTests(unittest.TestCase):
    """Which hops are eligible at all."""

    HTML = [("Content-Type", "text/html; charset=utf-8")]

    HTML_ACCEPT = "text/html,application/xhtml+xml,image/avif,*/*;q=0.8"

    def test_only_a_framed_get_with_fetch_metadata_counts(self):
        for dest in ("iframe", "frame", "embed", "object"):
            self.assertTrue(
                embed_route.framed_document("GET", dest, self.HTML_ACCEPT), dest)
        for dest in ("document", "empty", "script", "style", ""):
            self.assertFalse(
                embed_route.framed_document("GET", dest, self.HTML_ACCEPT), dest)
        self.assertFalse(
            embed_route.framed_document("POST", "iframe", self.HTML_ACCEPT))
        self.assertFalse(
            embed_route.framed_document("HEAD", "iframe", self.HTML_ACCEPT))

    def test_framed_media_is_not_a_framed_document(self):
        # `<object data>` and `<embed src>` share the destination with a frame
        # navigation but ask for `*/*`. Treating a framed PDF or video as a
        # document would cost it compression and weaken its strong ETag (so
        # If-Range resumption stops working) to look for a head it cannot have.
        for accept in ("*/*", "", "application/pdf", "text/*", "image/avif,*/*"):
            self.assertFalse(
                embed_route.framed_document("GET", "object", accept), accept)
        self.assertTrue(
            embed_route.framed_document("GET", "object", "text/html, */*"))

    def test_only_a_200_carries_a_document_worth_editing(self):
        for status in (204, 301, 304, 404, 500):
            self.assertFalse(embed_route.injectable(status, self.HTML), status)

    def test_only_text_html(self):
        for ctype in ("application/xhtml+xml", "application/json", "text/plain",
                      "text/html-fragment", ""):
            self.assertFalse(
                embed_route.injectable(200, [("content-type", ctype)]), ctype)
        self.assertTrue(
            embed_route.injectable(200, [("content-type", "TEXT/HTML")]))

    def test_a_compressed_or_partial_body_is_left_alone(self):
        self.assertFalse(embed_route.injectable(
            200, self.HTML + [("Content-Encoding", "gzip")]))
        self.assertTrue(embed_route.injectable(
            200, self.HTML + [("Content-Encoding", "identity")]))
        self.assertFalse(embed_route.injectable(
            200, self.HTML + [("Content-Range", "bytes 0-99/200")]))

    def test_utf16_is_left_alone(self):
        for charset in ("utf-16", "UTF-16LE", '"utf-16be"'):
            self.assertFalse(embed_route.injectable(
                200, [("content-type", f"text/html; charset={charset}")]),
                charset)

    def test_the_transport_charset_is_what_decides_the_prescan(self):
        self.assertEqual(embed_route.transport_charset(self.HTML), "utf-8")
        self.assertEqual(
            embed_route.transport_charset([("content-type", "text/html")]), "",
            "no charset parameter means the document's own <meta charset> is "
            "the only thing deciding how the app's text decodes")


class EtagTests(unittest.TestCase):
    """The cache dance that decides whether an app already in a browser's cache
    ever sees route memory at all."""

    def test_marking_round_trips_and_keeps_the_original_strength(self):
        for original in ('"abc"', 'W/"abc"', '"a-b-c.123"', '""'):
            marked = embed_route.mark_etag(original)
            self.assertTrue(marked.startswith('W/"ava1-'), marked)
            self.assertNotEqual(marked, original)
            self.assertEqual(embed_route.unmark_etag(marked), original)

    def test_an_etag_we_cannot_round_trip_is_left_exactly_as_it_is(self):
        self.assertEqual(embed_route.mark_etag("not-an-entity-tag"),
                         "not-an-entity-tag")

    def test_an_unmarked_tag_is_not_ours(self):
        for value in ('"abc"', 'W/"abc"', "*", "", 'W/"ava1-?abc"'):
            self.assertIsNone(embed_route.unmark_etag(value), value)

    def test_only_our_own_conditional_survives_the_hop(self):
        marked = embed_route.mark_etag('"v1"')
        also = embed_route.mark_etag('W/"v2"')
        self.assertEqual(embed_route.upstream_if_none_match(marked), '"v1"')
        self.assertEqual(embed_route.upstream_if_none_match(marked + ", " + also),
                         '"v1", W/"v2"')
        # Anything the browser did not get from US is dropped, so the app sends
        # a body we can inject into exactly once.
        for value in ('"v1"', "*", "", 'W/"v1", "v2"'):
            self.assertIsNone(
                embed_route.upstream_if_none_match(value), value)

    def test_a_minted_tag_stands_for_no_app_tag_at_all(self):
        minted = embed_route.mint_etag("Wed, 21 Oct 2015 07:28:00 GMT")
        self.assertTrue(embed_route.marked(minted))
        self.assertIsNone(embed_route.unmark_etag(minted),
                          "there is no app ETag inside a minted one, so nothing "
                          "of ours may ever be offered upstream as a validator")
        self.assertIsNone(embed_route.upstream_if_none_match(minted))
        self.assertEqual(minted,
                         embed_route.mint_etag(" Wed, 21 Oct 2015 07:28:00 GMT"),
                         "the 200 and the 304 that follows it have to mint the "
                         "same tag or the browser's cache entry churns")
        self.assertNotEqual(
            minted, embed_route.mint_etag("Thu, 22 Oct 2015 07:28:00 GMT"))

    def test_a_conditional_is_ours_when_any_marker_is_in_it(self):
        # What `If-Modified-Since` rides on: an app with Last-Modified and no
        # markable ETag has no other door back to a 304.
        self.assertTrue(embed_route.ours_conditional(
            embed_route.mint_etag("Wed, 21 Oct 2015 07:28:00 GMT")))
        self.assertTrue(embed_route.ours_conditional(
            '"other", ' + embed_route.mark_etag('"v1"')))
        for value in ('"v1"', "*", "", "W/\"v1\""):
            self.assertFalse(embed_route.ours_conditional(value), value)


class VaryTests(unittest.TestCase):
    """The body varies by `Sec-Fetch-Dest`, so the response has to say so."""

    def test_the_axis_is_added_to_whatever_the_app_already_named(self):
        self.assertEqual(embed_route.vary_with_dest(""), "Sec-Fetch-Dest")
        self.assertEqual(embed_route.vary_with_dest("Accept-Encoding"),
                         "Accept-Encoding, Sec-Fetch-Dest")
        self.assertEqual(
            embed_route.vary_with_dest("Accept-Encoding, sec-fetch-dest"),
            "Accept-Encoding, sec-fetch-dest",
            "already there: adding it twice says nothing new")
        self.assertEqual(embed_route.vary_with_dest("*"), "*",
                         "`*` already means no cache may reuse this; narrowing "
                         "it to one axis would be a widening")


class ManifestSwitchTests(unittest.TestCase):
    """`ui.route` is proxy behaviour in a manifest, not an owner-facing switch."""

    def test_the_modes_the_proxy_gates_on_are_the_modes_the_manifest_takes(self):
        self.assertEqual(connectors.ROUTE_MODES, ("auto", "self", "off"))
        self.assertEqual(embed_route.AUTO, connectors.ROUTE_MODES[0])

    def test_an_unrecognised_value_falls_back_to_the_default(self):
        for value in (None, "", "on", True, 7, "OFF "):
            self.assertIn(connectors._route_mode({"route": value}),
                          connectors.ROUTE_MODES)
        self.assertEqual(connectors._route_mode({}), "auto")
        self.assertEqual(connectors._route_mode({"route": "OFF "}), "off")
        self.assertEqual(connectors._route_mode({"route": "on"}), "auto")


if __name__ == "__main__":
    unittest.main()
