"""Put route memory INTO an embedded app's document, so every tile app has it.

**What the shell already does.** An `iframe` app that posts
`{type:'ava:navigation', cid, path}` is reopened on the page the owner left it
on: `frontend/src/lib/embedNavigation.ts` validates the message, `App.tsx`
mirrors it into the address and remembers it. That half is finished and is not
touched by anything here.

**Why the app cannot be asked to send it.** Only code inside the app's own
document can see an SPA route change — a `pushState` makes no HTTP request, so
the proxy never learns of it, and a cross-origin parent cannot read a frame's
location. Measured in a Playwright harness on Chromium and WebKit: after a
top-level reload the iframe always re-loads its `src` and its in-page location
is never restored. So route memory is a feature only the apps that opted in
have — today, exactly one of the owner's six.

**What this module does instead.** As the proxy streams an app's HTML through,
it splices one `<script>` into the head. The script (`assets/app_route.js`)
speaks the protocol the shell already implements and validates, so nothing in
the frontend changes and a self-reporting app keeps working exactly as it did.

Nothing here imports FastAPI or touches a request object: it is the decision
half — *where* a tag may go, *whether* a Content-Security-Policy leaves room for
it, and *which* responses are eligible — so it can be unit-tested without a
bridge. `phone_bridge` owns the wiring.

**The CSP rule, stated once because it is the easy thing to get wrong:** an
app's policy is never edited, widened or dropped. We read it and decide what we
are allowed to add. Adopting a nonce the app has already published to its own
same-origin scripts weakens nothing — the policy is exactly as strong for
everything else — while rewriting one to admit us would quietly make the app
less safe than its author wrote it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from . import apps_origin

_HERE = os.path.dirname(os.path.abspath(__file__))
SHIM_PATH = os.path.join(_HERE, "assets", "app_route.js")

with open(SHIM_PATH, encoding="utf-8") as _f:
    _SHIM_SRC = _f.read()

#: `ui.route` in a connector manifest. Mirrors `connectors.ROUTE_MODES`; kept as
#: a name here so the proxy gate reads as a sentence rather than a string
#: literal, and so a test can pin the two lists against each other.
AUTO = "auto"

#: How far into a document the insertion point is looked for before the body is
#: passed through untouched. A head is a few hundred bytes; anything that has
#: not resolved within 64 KiB is not a document we understand, and guessing at
#: that point is worse than leaving the app exactly as it was.
SCAN_LIMIT = 64 * 1024

#: `injection_point` has not seen enough bytes to decide. Distinct from `None`,
#: which is the decision "never".
PENDING = -1

#: How far into a document a browser reads looking for a `<meta charset>` before
#: it commits to an encoding — 1024 bytes, by the HTML standard's prescan and by
#: every implementation of it. Load-bearing only when the transport named no
#: charset of its own, because a `Content-Type` that names one outranks the
#: document: then a ~9 KB tag spliced in AHEAD of the declaration pushes it out
#: of the window and the app's own text decodes as mojibake — but only when
#: framed in Ava, which is the worst shape a bug of ours can take.
PRESCAN = 1024

#: Where a framed navigation may be rendered. `document` is a TOP-LEVEL
#: navigation and belongs to the shell bounce; `empty` is the app's own
#: `fetch`, and splicing a script into an HTML fragment the app is about to
#: hand to `innerHTML` would run our code somewhere nobody asked for it.
_FRAMED_DESTS = frozenset({"iframe", "frame", "embed", "object"})

#: base64 or base64url, the two spellings a CSP nonce is generated in. Bounded
#: at both ends: a one-character "nonce" is not one, and a policy long enough to
#: hold a 256-character token is not being parsed here for anyone's benefit.
_NONCE_RE = re.compile(r"^[A-Za-z0-9+/_=-]{8,256}$")

#: Connector ids are `[a-z][a-z0-9_-]{1,31}` by the time a manifest exists, and
#: this is the last place that fact is load-bearing: the id goes into a `src`
#: attribute and into a JS string literal.
_CID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def _js(value: str) -> str:
    """A JS literal for `value` that cannot end the `<script>` that carries it.

    `</` inside a string literal closes the element as far as the HTML parser is
    concerned; the escape is invisible to JSON and to JS. Same treatment
    `apps_origin.reconnect_page` gives its own inline script.
    """
    return json.dumps(value).replace("</", "<\\/")


def shim(cid: str) -> str:
    """The script source for one connector, ready to serve or to inline."""
    return (_SHIM_SRC
            .replace("__CID__", _js(cid))
            .replace("__SHELL__", _js(apps_origin.shell_origin())))


def script_url(cid: str) -> str:
    """Where the external form of the shim lives.

    ROOT-relative, not relative-to-here: an app may install a runtime
    `<base href>`, and a relative `src` would then resolve against whatever the
    app chose rather than against the proxy mount.
    """
    return f"/apps/{cid}/.ava/route.js"


def script_tag(cid: str, headers) -> bytes | None:
    """The bytes to splice in, or None when the app's CSP leaves no room.

    `headers` is any iterable of `(name, value)` pairs from the upstream
    response — repeated names included, which is why it is not a mapping.
    """
    if not _CID_RE.match(str(cid or "")):
        return None
    kind, nonce = csp_decision(headers)
    if kind == "blocked":
        return None
    if kind == "external":
        return f'<script src="{script_url(cid)}"></script>'.encode()
    attr = f' nonce="{nonce}"' if nonce else ""
    return f"<script{attr}>{shim(cid)}</script>".encode()


# --- Content-Security-Policy -------------------------------------------------

def _policies(headers) -> list[str]:
    """Every ENFORCING policy, one string each.

    `Content-Security-Policy-Report-Only` is deliberately not here: it reports,
    it does not block, and treating it as a refusal would switch route memory
    off for an app that is merely measuring a policy it has not adopted. One
    header may carry several policies separated by commas, and several headers
    may each carry more; every one of them has to be satisfied.
    """
    out = []
    for name, value in headers:
        if str(name).strip().lower() != "content-security-policy":
            continue
        for policy in str(value).split(","):
            if policy.strip():
                out.append(policy.strip())
    return out


def _capabilities(policy: str) -> dict:
    """What one policy leaves room for: `{inline, nonces, external}`.

    `inline` is "an inline script with no nonce would run" — CSP3 makes
    `'unsafe-inline'` a no-op the moment a nonce or a hash is present, or under
    `'strict-dynamic'`, so those cancel it here too rather than being read as
    permission. `external` is "a script element on this document's own origin
    would run", which is where `script_url` lives; `'strict-dynamic'` cancels
    that as well, because it makes host sources (`'self'` included) ignored.
    """
    directives: dict[str, list[str]] = {}
    for part in policy.split(";"):
        tokens = part.split()
        if tokens:
            directives.setdefault(tokens[0].lower(), tokens[1:])
    if "sandbox" in directives:
        # A sandbox policy re-makes the document's origin and can take scripting
        # away entirely. Whatever it allows, it is not a document to add code to.
        return {"inline": False, "nonces": set(), "external": False}
    sources = None
    for key in ("script-src-elem", "script-src", "default-src"):
        if key in directives:
            sources = directives[key]
            break
    if sources is None:
        return {"inline": True, "nonces": set(), "external": True}   # not a script policy
    lowered = [t.lower() for t in sources]
    if "'none'" in lowered:
        return {"inline": False, "nonces": set(), "external": False}
    strict = "'strict-dynamic'" in lowered
    hashed = any(t.startswith(("'sha256-", "'sha384-", "'sha512-")) for t in lowered)
    # A nonce SOURCE and a VALID nonce are different questions. A malformed one
    # still disables `'unsafe-inline'` for the browser, so it must disable our
    # reading of it too — otherwise we would inline a script the browser drops.
    nonced = [t for t in sources if t.lower().startswith("'nonce-") and t.endswith("'")]
    nonces = {t[len("'nonce-"):-1] for t in nonced}
    return {
        "inline": ("'unsafe-inline'" in lowered
                   and not nonced and not hashed and not strict),
        "nonces": {n for n in nonces if _NONCE_RE.match(n)},
        "external": not strict and ("'self'" in lowered or "*" in lowered),
    }


def csp_decision(headers) -> tuple[str, str | None]:
    """`("inline", None) | ("inline", nonce) | ("external", None) | ("blocked", None)`.

    Preference order is "least new surface first": a plain inline tag, else one
    carrying a nonce the app already trusts, else a same-origin file, else
    nothing. EVERY enforcing policy has to admit the choice, so two policies
    naming different nonces leave no inline tag either of them would run — and
    then only a same-origin file both of them allow can get in.
    """
    caps = [_capabilities(p) for p in _policies(headers)]
    if not caps:
        return "inline", None
    if all(c["inline"] for c in caps):
        return "inline", None
    demanding = [c for c in caps if not c["inline"]]
    if all(c["nonces"] for c in demanding):
        common = set.intersection(*(c["nonces"] for c in demanding))
        if common:
            return "inline", sorted(common)[0]
    if all(c["external"] for c in caps):
        return "external", None
    return "blocked", None


# --- Where the tag goes ------------------------------------------------------

def _tag_end(buf: bytes, start: int) -> int:
    """Index just past this tag's `>`, or -1 when the buffer stops inside it.

    Quoted attribute values are skipped, because `content=">"` is legal and a
    plain `find(b'>')` would cut such a tag in half — which matters precisely
    for the two tags this scanner must recognise and stop at.
    """
    quote = b""
    i = start
    while i < len(buf):
        ch = buf[i:i + 1]
        if quote:
            if ch == quote:
                quote = b""
        elif ch in (b'"', b"'"):
            quote = ch
        elif ch == b">":
            return i + 1
        i += 1
    return -1


_META_CHARSET = re.compile(rb"\bcharset\s*=")
_META_EQUIV = re.compile(rb"\bhttp-equiv\s*=\s*[\"']?\s*([a-z-]+)")
# START tags only. `</head>` must STOP the scan, not advance past it: the tag
# belongs inside the head it closes, not between the head and the body.
_TAG_NAME = re.compile(rb"^<\s*([a-z][a-z0-9]*)")

#: Head elements with text inside them. Only `_charset_ahead` walks past a tag,
#: so only it has to know that their content is not the head ending.
_TEXT_ELEMENTS = frozenset({b"title", b"style", b"script", b"noscript",
                            b"textarea"})


def _scan(buf: bytes, complete: bool, limit: int) -> tuple[int | None, bool]:
    """`(offset, a charset declaration is already behind it)`.

    The offset is the one `injection_point` documents; the flag is what tells it
    whether the encoding prescan still has something left to protect.

    The order is fixed by what breaks otherwise:

      * NEVER before the doctype — a tag ahead of it puts the whole page in
        quirks mode, which changes the app's layout, not just ours.
      * After a leading `<meta charset>` / `<meta http-equiv="content-type">`
        when one is there, so the declaration stays inside the first 1024 bytes
        the encoding sniffer reads. Inserting ahead of it can push it out and
        change how the app's own text decodes.
      * Otherwise just after `<head…>`, else just after `<html…>`, else just
        before `<body…>`. First in `<head>` is the requirement the shim states:
        Next.js's app router captures `history.pushState` at startup, so a
        wrapper installed after the app's bundle is never called.
      * Never after a `<base>` or a `<meta http-equiv="Content-Security-Policy">`
        — both change the meaning of what follows them, and getting in first is
        the only position where neither can.

    Comments are skipped whole, so a `<head` written inside one cannot move the
    insertion point, and a scan that has not resolved within `limit` gives up
    rather than guessing.
    """
    if buf[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return None, False   # UTF-16: ASCII bytes spliced in would be garbage
    at = i = 3 if buf[:3] == b"\xef\xbb\xbf" else 0   # a BOM stays the first bytes
    charset = False

    def undecided() -> tuple[int | None, bool]:
        """Out of bytes: wait for more, unless that is all there will be."""
        if complete:
            return at, charset
        return (None, False) if len(buf) >= limit else (PENDING, False)

    while True:
        while i < len(buf) and buf[i:i + 1] in b" \t\r\n\f":
            i += 1
        if i >= limit:
            return None, False
        if i >= len(buf):
            return undecided()
        if buf[i:i + 1] != b"<":
            return at, charset   # text, or an implied <body> — get in ahead of it
        if buf[i:i + 4] == b"<!--":
            end = buf.find(b"-->", i + 4)
            if end < 0:
                return undecided()
            at = i = end + 3
            continue
        end = _tag_end(buf, i)
        if end < 0:
            return undecided()
        raw = buf[i:end].lower()
        if raw.startswith(b"<!") or raw.startswith(b"<?"):
            at = i = end     # doctype, CDATA-ish junk, an XML declaration
            continue
        match = _TAG_NAME.match(raw)
        name = match.group(1) if match else b""
        if name in (b"html", b"head"):
            at = i = end
            continue
        if name == b"meta":
            equiv = _META_EQUIV.search(raw)
            kind = equiv.group(1) if equiv else b""
            if kind == b"content-type" or (not equiv and _META_CHARSET.search(raw)):
                at = i = end
                charset = True
                continue
        return at, charset   # anything else: <base>, a meta CSP, <body>, <title>…


def _charset_ahead(buf: bytes, start: int, complete: bool) -> int | None:
    """Just past a charset declaration lying between `start` and the end of the
    prescan window, `None` when there is none there, or `PENDING` for more bytes.

    Only asked when the transport named no charset. Tags are stepped over
    WITHOUT moving anything: the question is not where we may insert, it is
    whether inserting at `start` would shove a declaration the browser is about
    to read past the point it stops reading. `</head>`, a `<body>` or loose text
    end the search, and so does the window — a declaration already outside it is
    one the browser never saw, so moving it costs nothing.
    """
    i = start
    while i < len(buf) and i < PRESCAN:
        ch = buf[i:i + 1]
        if ch != b"<":
            if ch in b" \t\r\n\f":
                i += 1
                continue
            return None      # text: the head is over
        if buf[i:i + 4] == b"<!--":
            end = buf.find(b"-->", i + 4)
            if end < 0:
                break
            i = end + 3
            continue
        end = _tag_end(buf, i)
        if end < 0:
            break
        raw = buf[i:end].lower()
        match = _TAG_NAME.match(raw)
        name = match.group(1) if match else b""
        if name == b"body" or raw.startswith(b"</head"):
            return None
        if name == b"meta":
            equiv = _META_EQUIV.search(raw)
            kind = equiv.group(1) if equiv else b""
            if kind == b"content-type" or (not equiv and _META_CHARSET.search(raw)):
                return end
        if name in _TEXT_ELEMENTS:
            # `<title>My App</title>` is the shape that puts a charset meta
            # second, so its CONTENT must not be read as the head ending.
            close = buf.find(b"</" + name, end)
            end = _tag_end(buf, close) if close >= 0 else -1
            if end < 0:
                break
        i = end
    return None if complete or len(buf) >= PRESCAN else PENDING


def injection_point(buf: bytes, *, complete: bool = False,
                    limit: int = SCAN_LIMIT, prescan: bool = False) -> int | None:
    """Byte offset to insert at, `None` for "leave this document alone", or
    `PENDING` when the answer needs more bytes than `buf` holds.

    Where the offset comes from is `_scan`. `prescan` says the response's
    `Content-Type` named no charset, which makes a `<meta charset>` in the
    document the only thing deciding how the app's own text decodes: the tag
    then goes after that declaration wherever in the head it is, not only when
    it is the first thing there. An app whose head opens with a `<title>` or a
    viewport meta is the common shape, and splicing ~9 KB in front of its
    charset line renders the whole app as mojibake — in Ava's frame and nowhere
    else, which is exactly the bug nobody would think to report against Ava.

    Landing after a later declaration costs the "first in `<head>`" property for
    that app. Encoding wins: a router whose `pushState` we miss reopens on its
    home page, while a document decoded wrong is unreadable.
    """
    at, charset = _scan(buf, complete, limit)
    if not prescan or charset or at is None or at == PENDING:
        return at
    ahead = _charset_ahead(buf, at, complete)
    if ahead == PENDING:
        return PENDING
    return at if ahead is None else ahead


class Injector:
    """Splice `tag` into an HTML stream, buffering only until the point is known.

    A state machine rather than a `replace` over the whole body because every
    Next.js app streams its document: holding it all to scan it would turn an
    app that paints in 200ms into one that paints when the last byte lands. Once
    the insertion point is decided — which is within the first chunk for every
    real document — every later chunk is passed straight through untouched.
    """

    def __init__(self, tag: bytes, limit: int = SCAN_LIMIT, *,
                 prescan: bool = False):
        self._tag = tag
        self._limit = limit
        self._prescan = prescan
        self._buf = b""
        self._settled = False

    def feed(self, chunk: bytes) -> bytes:
        if self._settled:
            return chunk
        self._buf += chunk
        point = injection_point(self._buf, limit=self._limit,
                                prescan=self._prescan)
        if point == PENDING:
            return b""
        self._settled = True
        out, self._buf = self._buf, b""
        return out if point is None else out[:point] + self._tag + out[point:]

    def finish(self) -> bytes:
        """Whatever is still held back, with the tag if a point was found.

        A document that ends before the scanner could decide still gets the tag
        at the best offset found — `<html>` with no `<head>` and no `<body>` is
        a real thing apps serve.
        """
        if self._settled:
            return b""
        self._settled = True
        out, self._buf = self._buf, b""
        point = injection_point(out, complete=True, limit=self._limit,
                                prescan=self._prescan)
        if point is None or point == PENDING:
            return out
        return out[:point] + self._tag + out[point:]


# --- Which hops are eligible -------------------------------------------------

def framed_document(method: str, dest: str, accept: str) -> bool:
    """A GET the browser will render AS a frame's document.

    Fetch Metadata must be PRESENT. `apps_origin.wants_frame_document` falls
    back to `Accept` when it is missing, because its job is picking an error
    page's media type and guessing wrong there only changes what an error looks
    like. Guessing wrong here edits a body, so no header means no.

    `Accept` has to name `text/html` as well, because `Sec-Fetch-Dest` alone
    does not separate a frame's DOCUMENT from media: an `<iframe src>`
    navigation sends `text/html,…` while `<object data>` and `<embed src>` send
    `*/*` under the same `embed`/`object` destination. Without this clause a
    framed PDF or video would transfer uncompressed forever, lose the strong
    ETag that makes `If-Range` resumption work, and gain a conditional-header
    strip — all to look for a `<head>` it can never have.
    """
    if str(method or "").upper() != "GET":
        return False
    if str(dest or "").lower() not in _FRAMED_DESTS:
        return False
    return any(part.split(";")[0].strip().lower() == "text/html"
               for part in str(accept or "").split(","))


def injectable(status: int, headers) -> bool:
    """Is THIS response one we may add a script to?

    The connector's `ui.route` is NOT asked here — that is a question about the
    app, settled once per hop by the caller, and folding it in made this read
    like a response check that is not one.

    Every clause is a way of not corrupting something:
      * 200 only — a 204/304 has no body, and a 3xx/4xx body is the app's own
        error page, which is not the page route memory is for.
      * `text/html` exactly — never `application/xhtml+xml`, where an unclosed
        tag is a fatal parse error rather than a tolerated one.
      * no `Content-Encoding` — gzipped bytes are not scannable, and the proxy
        forwards encodings verbatim. `_upstream_headers` asks for `identity` on
        exactly these hops so this clause is satisfiable.
      * no `Content-Range` — a byte range of a document is not a document.
      * not UTF-16 — the tag is ASCII bytes and would land as mojibake.
    """
    if status != 200:
        return False
    ctype = encoding = crange = ""
    for name, value in headers:
        lower = str(name).strip().lower()
        if lower == "content-type":
            ctype = str(value)
        elif lower == "content-encoding":
            encoding = str(value)
        elif lower == "content-range":
            crange = str(value)
    if crange.strip() or encoding.strip().lower() not in ("", "identity"):
        return False
    if ctype.lower().split(";")[0].strip() != "text/html":
        return False
    return not transport_charset(headers).startswith("utf-16")


def transport_charset(headers) -> str:
    """The charset the `Content-Type` named, lowercased, or `""` for none.

    Two callers, two questions off one answer: `injectable` refuses UTF-16, and
    `""` is what tells the injector that the document's own `<meta charset>` is
    the only thing deciding how the app's text decodes (see `PRESCAN`).
    """
    ctype = ""
    for name, value in headers:
        if str(name).strip().lower() == "content-type":
            ctype = str(value)
    for param in ctype.lower().split(";")[1:]:
        key, _, value = param.partition("=")
        if key.strip() == "charset":
            return value.strip().strip('"')
    return ""


# --- Caching -----------------------------------------------------------------
#
# The case that decides whether any of this reaches the owner. An app whose
# entry document carries an ETag is revalidated, not refetched: a browser
# holding a copy cached BEFORE route memory shipped would offer that ETag, the
# app would answer 304, and the frame would keep running a document with no shim
# in it — forever, because nothing about it ever changes. So the ETag we serve
# is namespaced while injection is on, an incoming one is unmarked before it
# goes upstream, and an UNMARKED one is dropped so the app is made to send a
# body we can inject into, exactly once. Nothing else about the app's cache
# headers is touched: `private, no-store` survives verbatim and an unversioned
# page still gets exactly `no-cache` (tests/test_app_proxy_contract.py).

_ETAG_MARK = "ava1-"


def mark_etag(value: str) -> str:
    """`"abc"` -> `W/"ava1-sabc"`; `W/"abc"` -> `W/"ava1-wabc"`.

    Always weak on the way out, because the body no longer matches the app's
    byte for byte. The flag letter carries the original's own strength so
    `unmark_etag` hands the app back exactly the tag it issued — a comparison
    that has to be weak by RFC 9110 anyway, but not every server implements it
    that way and this costs one character.
    """
    raw = value.strip()
    weak = raw[:2].upper() == "W/"
    if weak:
        raw = raw[2:].strip()
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return value        # not an entity-tag we can round-trip; leave it be
    return f'W/"{_ETAG_MARK}{"w" if weak else "s"}{raw[1:-1]}"'


def _flagged(value: str) -> tuple[str, str] | None:
    """`(flag letter, the rest)` for a tag of ours, else None."""
    raw = value.strip()
    if raw[:2].upper() == "W/":
        raw = raw[2:].strip()
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return None
    inner = raw[1:-1]
    if not inner.startswith(_ETAG_MARK):
        return None
    mark = len(_ETAG_MARK)
    flag = inner[mark:mark + 1]
    return (flag, inner[mark + 1:]) if flag in ("w", "s", "m") else None


def marked(value: str) -> bool:
    """Is this an ETag WE issued — round-trippable or minted?"""
    return _flagged(value) is not None


def unmark_etag(value: str) -> str | None:
    """The app's own ETag back out of a marked one, or None when it is not ours.

    A MINTED tag answers None too: there is no app tag inside it, so there is
    nothing to hand back, and offering one the app never issued would be asking
    it to validate against a value of ours.
    """
    found = _flagged(value)
    if not found or found[0] == "m":
        return None
    flag, rest = found
    return f'W/"{rest}"' if flag == "w" else f'"{rest}"'


def mint_etag(last_modified: str) -> str:
    """A marked weak ETag of our own, for a document whose app gave us none.

    Two ordinary shapes arrive with no validator we can mark — an app sending
    `Last-Modified` and no ETag (python's http.server and most small static
    servers), and an app whose ETag is not a quoted entity-tag, which
    `mark_etag` cannot round-trip. Without this both would lose revalidation
    permanently: the conditional headers are dropped on every injecting hop, and
    only a MARKED `If-None-Match` coming back buys them a way through, so every
    single frame load would be a full uncompressed refetch.

    The flag letter says minted, so the tag never travels upstream. What it buys
    is the other door: a browser handed an ETag keeps the `Last-Modified` beside
    it and offers both next time, and seeing one of ours is what lets
    `If-Modified-Since` through to the app so it can still answer 304. Derived
    from the `Last-Modified` value, so the same document mints the same tag on
    the 200 and on the 304 that follows it.
    """
    digest = hashlib.sha256(str(last_modified).strip().encode("utf-8", "replace"))
    return f'W/"{_ETAG_MARK}m{digest.hexdigest()[:16]}"'


def upstream_if_none_match(value: str) -> str | None:
    """The `If-None-Match` to forward on an injecting hop: ours, unmarked.

    Anything the browser did not get from us is dropped rather than forwarded,
    so the app answers with a full body once and the injected copy replaces the
    cached one. `*` is dropped by the same rule, and so is a minted tag, which
    stands for no tag of the app's at all.
    """
    kept = [original for part in str(value or "").split(",")
            if (original := unmark_etag(part)) is not None]
    return ", ".join(kept) if kept else None


def ours_conditional(value: str) -> bool:
    """Did this `If-None-Match` come from a copy WE served?

    The question `If-Modified-Since` rides on. A marked tag — round-trippable or
    minted — means the cached copy already carries the shim, so letting the
    app's other validator through can only earn a 304 against a copy that is
    already correct. An unmarked one is a pre-injection copy and every
    conditional with it is dropped, so the app sends a body we can inject into.
    """
    return any(marked(part) for part in str(value or "").split(","))


def vary_with_dest(value: str = "") -> str:
    """`Vary` with `Sec-Fetch-Dest` added to whatever the app already named.

    On an injecting hop the body genuinely varies by `Sec-Fetch-Dest` — the
    frame's document carries our script and the app's own `fetch` of the same
    URL does not. A cache keyed on the URL alone would store one for both: an
    app reading its own HTML could be handed Ava's script inside it, or a frame
    could be served an uninjected copy and route memory would be silently off.
    Merged, never replaced: dropping the app's own `Vary` would make a cache
    serve a response negotiated for somebody else.
    """
    parts = [p.strip() for p in str(value or "").split(",") if p.strip()]
    if any(p == "*" or p.lower() == "sec-fetch-dest" for p in parts):
        return ", ".join(parts)
    return ", ".join(parts + ["Sec-Fetch-Dest"])
