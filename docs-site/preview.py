#!/usr/bin/env python3
"""Serve the BUILT docs site the way GitHub Pages will serve it.

    python docs-site/sync.py && mkdocs build -f docs-site/mkdocs.yml
    python docs-site/preview.py                 # -> http://127.0.0.1:8099/Ava/

Why this exists rather than just `mkdocs serve`:

  * Range requests. `mkdocs serve` and `python -m http.server` both answer a
    `Range:` header with a plain `200` and the whole file, where a browser
    asking to seek inside a large asset needs a `206`. GitHub Pages does
    support Range, so reviewing the site on a server that does not is reviewing
    a site that behaves differently from the published one.

  * The path prefix. `site_url` puts the site at `/Ava/`, not `/`. `mkdocs
    serve` honours that (its root just redirects), but a bare
    `python -m http.server` in `docs-site/site` does not, so every absolute
    link 404s and the site looks half-broken for no reason.

  * It serves `site/` — the actual artifact `upload-pages-artifact` publishes,
    built by `mkdocs build --strict`. `mkdocs serve` renders from a separate
    in-memory build, so it cannot show you a stale or failed build.

Binds to loopback by default. `--host 0.0.0.0` (or a tailnet IP) to review from
a phone; the site is static and read-only, but it is still the whole pre-launch
site, so do not do that on a network you do not trust.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
SITE = HERE / "site"
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


class PagesHandler(SimpleHTTPRequestHandler):
    """Static handler with Range support and a mount prefix."""

    prefix = "/"

    def end_headers(self) -> None:
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        # Strip the mount prefix before the base class resolves it on disk.
        split = urlsplit(path)
        p = split.path
        if self.prefix != "/":
            base = self.prefix.rstrip("/")
            if p == base:
                p = "/"
            elif p.startswith(self.prefix):
                p = p[len(base):]
        rest = f"?{split.query}" if split.query else ""
        return super().translate_path(p + rest)

    def send_head(self):
        # Send anyone who lands on / to the real site root, the way Pages does.
        if self.prefix != "/" and urlsplit(self.path).path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", self.prefix)
            self.end_headers()
            return None

        self._remaining = None
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not rng or os.path.isdir(path):
            return super().send_head()
        m = RANGE_RE.match(rng.strip())
        if not m or (not m.group(1) and not m.group(2)):
            return super().send_head()  # multi-range: fall back to the whole file
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        size = os.fstat(f.fileno()).st_size
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:  # suffix form: bytes=-N
            start = max(0, size - int(m.group(2)))
            end = size - 1
        end = min(end, size - 1)
        if start >= size:
            f.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            super(SimpleHTTPRequestHandler, self).end_headers()
            return None
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        f.seek(start)
        self._remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        while remaining > 0:
            chunk = source.read(min(64 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)
        self._remaining = None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument(
        "--prefix",
        default=None,
        help="mount path (default: the path from mkdocs.yml's site_url, e.g. /Ava/)",
    )
    args = ap.parse_args()

    if not (SITE / "index.html").exists():
        print(
            f"error: {SITE} has no index.html — build it first:\n"
            "  python docs-site/sync.py && mkdocs build --strict -f docs-site/mkdocs.yml",
            file=sys.stderr,
        )
        return 1

    prefix = args.prefix
    if prefix is None:
        # Read site_url without importing mkdocs (this script has no deps).
        prefix = "/"
        text = (HERE / "mkdocs.yml").read_text(encoding="utf-8")
        m = re.search(r"^site_url:\s*(\S+)", text, re.M)
        if m:
            prefix = urlsplit(m.group(1).strip("'\"")).path or "/"
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if not prefix.endswith("/"):
        prefix += "/"

    handler = partial(PagesHandler, directory=str(SITE))
    PagesHandler.prefix = prefix
    server = ThreadingHTTPServer((args.host, args.port), handler)
    host = args.host if ":" not in args.host else f"[{args.host}]"
    print(f"Serving {SITE} at http://{host}:{args.port}{prefix}  (Range supported — video seeking works)")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
