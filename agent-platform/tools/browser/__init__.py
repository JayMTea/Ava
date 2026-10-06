"""Fetch a web page as text, under policies/network.yaml.

Every URL - including each redirect hop - is checked against the domain allowlist and
the private-network (SSRF) block before a connection is made. Responses are size-capped
and HTML is reduced to text. The content is untrusted: the executor labels it so.
"""

from __future__ import annotations

import urllib.request
from html.parser import HTMLParser
from typing import Any

from runtime.types import ToolContext, ToolError

USER_AGENT = "agent-platform/1.0 (+reference browser tool)"


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _opener(ctx: ToolContext) -> urllib.request.OpenerDirector:
    class CheckedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
            ctx.policy.check_url(newurl)  # raises PolicyViolation on a disallowed hop
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return urllib.request.build_opener(CheckedRedirect)


def fetch(args: dict[str, Any], ctx: ToolContext) -> str:
    url = args["url"]
    ctx.policy.check_url(url)
    limit = int(ctx.policy.network.get("max_response_bytes", 2_000_000))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _opener(ctx).open(request, timeout=20) as resp:
            body = resp.read(limit + 1)
            content_type = resp.headers.get("Content-Type", "")
            charset = resp.headers.get_content_charset() or "utf-8"
    except OSError as exc:
        raise ToolError(f"fetch failed: {exc}") from exc
    truncated = len(body) > limit
    text = body[:limit].decode(charset, errors="replace")
    if "html" in content_type:
        parser = _Text()
        parser.feed(text)
        text = "\n".join(parser.parts)
    note = "[response truncated]\n" if truncated else ""
    return f"URL: {url}\n{note}{text[: args.get('max_chars', 20000)]}"
