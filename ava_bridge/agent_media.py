"""Resolve agent/tool-produced media references to same-origin, seekable URLs.

A finished turn — or a tool result — can name media in half a dozen shapes: an
http(s) URL, a `data:` URI, a sandbox-local path (`/sandbox/...`, `file://`,
`~/...`), OpenClaw's `media://inbound/<id>` or `/__openclaw__/assistant-media?…`
routes. None of those is something Ava's browser can fetch from Ava's own origin
and seek inside. This module turns each into a file under `UPLOAD_DIR`, served
at `/uploads/<name>` — a mount that already honours HTTP Range (so `<video>`
seeks) and is cookie-gated exactly like the chat.

Best-effort by contract, because the whole point is that media must never break
a reply:

  * a ref that cannot be turned into real bytes is DROPPED, never returned as a
    broken player;
  * a public http(s) URL that cannot be downloaded host-side is passed through
    unchanged as a last resort (Ava serves no CSP, so a cross-origin `<img>` /
    `<video>` still loads);
  * a ref already on Ava's origin (`/uploads/…`, `/apps/<id>/…`, `/media/…`, a
    bare relative path) is passed through untouched.

Nothing here runs on the event loop: `turns._finish_turn` calls it from the
turn's worker thread, after the reply is in hand.
"""
from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
from urllib.parse import urlsplit

import requests

from .config import UPLOAD_DIR

# A generous ceiling for one produced clip. Downloads stream to disk against a
# running byte count, so this bounds disk, not memory.
_MAX_MEDIA_BYTES = int(os.environ.get("AVA_MAX_MEDIA_MB", "200")) * 1024 * 1024
_DOWNLOAD_TIMEOUT = 30

# Kinds the chat can render. Everything else is a downloadable file card.
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".ogv", ".mkv"}
_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".opus"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
               ".avif", ".tif", ".tiff", ".heic"}

# A ref whose URL starts with one of these is already fetchable from Ava's
# origin — hand it to the browser unchanged.
_SAME_ORIGIN_PREFIXES = ("/uploads/", "/apps/", "/media/", "/assets/")

_DATA_URI = re.compile(r"^data:([^;,]*)(;base64)?,(.*)$", re.DOTALL)


def resolve_refs(rt, refs: list[dict] | None) -> list[dict]:
    """Resolve a list of raw refs; drop anything that cannot be made playable.

    De-dupes by resolved URL so the same clip named twice (a `MEDIA:` line and a
    `mediaUrls` entry) renders once.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        got = resolve_ref(rt, ref)
        if got and got.get("url") and got["url"] not in seen:
            seen.add(got["url"])
            out.append(got)
    return out


def resolve_ref(rt, ref: dict) -> dict | None:
    """One raw ref → a rendered MediaRef {url, kind, filename?, mime?}, or None."""
    url = str(ref.get("url") or "").strip()
    if not url:
        return None
    mime = str(ref.get("mime") or "").strip() or None
    filename = str(ref.get("filename") or "").strip() or None

    # 1. Already fetchable from Ava's own origin — hand it over as-is. Only the
    #    explicit mount prefixes: a bare absolute path like `/sandbox/...` is a
    #    filesystem path, not a URL on Ava's origin, and belongs in step 4.
    if url.startswith(_SAME_ORIGIN_PREFIXES):
        return _shape(url, mime, filename)

    # 2. data: URI — decode and store.
    m = _DATA_URI.match(url)
    if m:
        try:
            raw = base64.b64decode(m.group(3)) if m.group(2) else m.group(3).encode()
        except Exception:  # noqa: BLE001
            return None
        return _store(raw, mime or (m.group(1) or None), filename)

    scheme = urlsplit(url).scheme.lower()

    # 3. Public http(s) — download host-side (so it is same-origin and seekable);
    #    on failure, pass the public URL through unchanged.
    if scheme in ("http", "https") and "__openclaw__" not in url:
        stored = _download(url, mime, filename)
        if stored:
            return stored
        return _shape(url, mime, filename)

    # 4. Sandbox-local / gateway-served — fetch the bytes over the gateway.
    raw, got_mime = _fetch_via_gateway(rt, url)
    if raw:
        return _store(raw, mime or got_mime, filename or _basename(url))
    return None


# --------------------------------------------------------------------------- #
# Storage + shaping
# --------------------------------------------------------------------------- #
def _store(raw: bytes, mime: str | None, filename: str | None) -> dict | None:
    if not raw or len(raw) > _MAX_MEDIA_BYTES:
        return None
    ext = _ext_for(mime, filename) or ".bin"
    name = f"agent_{hashlib.sha256(raw).hexdigest()[:16]}{ext}"
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        dest = os.path.join(UPLOAD_DIR, name)
        if not os.path.exists(dest):
            with open(dest, "wb") as f:
                f.write(raw)
    except OSError:
        return None
    return _shape(f"/uploads/{name}", mime, filename or name)


def _shape(url: str, mime: str | None, filename: str | None) -> dict:
    ref: dict = {"url": url, "kind": _kind_for(mime, url, filename)}
    if filename:
        ref["filename"] = filename
    if mime:
        ref["mime"] = mime
    return ref


def _download(url: str, mime: str | None, filename: str | None) -> dict | None:
    try:
        with requests.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT,
                          allow_redirects=True) as r:
            if r.status_code != 200:
                return None
            got_mime = mime or (r.headers.get("content-type") or "").split(";")[0].strip() or None
            chunks: list[bytes] = []
            total = 0
            for chunk in r.iter_content(65536):
                total += len(chunk)
                if total > _MAX_MEDIA_BYTES:
                    return None
                chunks.append(chunk)
    except requests.RequestException:
        return None
    return _store(b"".join(chunks), got_mime, filename or _basename(url))


def _fetch_via_gateway(rt, ref: str):
    """Fetch a sandbox-local media reference's bytes over the gateway socket.

    OpenClaw exposes `artifacts.download` / `sessions.files.get` / `agents.files.get`
    (verified present on this gateway, 2026-08-24). Their exact param and return
    shapes are not pinned here, so this tries the plausible ones and reads bytes
    from base64/content, returning (None, None) on any failure — which drops the
    ref rather than rendering a broken player. This is the one path with no live
    end-to-end coverage (no media-producing turn exists in the sandbox today).
    """
    rpc = getattr(rt, "rpc", None)
    methods = getattr(rt, "rpc_methods", lambda: frozenset())()
    if not callable(rpc):
        return None, None
    source = _sandbox_source(ref)
    attempts = [
        ("artifacts.download", {"source": source}),
        ("artifacts.download", {"path": source}),
        ("sessions.files.get", {"path": source}),
        ("agents.files.get", {"name": source}),
    ]
    for method, params in attempts:
        if method not in methods:
            continue
        try:
            got = rpc(method, params, timeout=30.0)
        except Exception:  # noqa: BLE001 — any gateway error just drops the ref
            continue
        raw, mime = _bytes_from_rpc(got)
        if raw:
            return raw, mime
    return None, None


def _bytes_from_rpc(got):
    if not isinstance(got, dict):
        return None, None
    holder = got.get("file") if isinstance(got.get("file"), dict) else got
    mime = None
    for k in ("mimeType", "mime", "contentType"):
        v = holder.get(k)
        if isinstance(v, str) and v.strip():
            mime = v.strip()
            break
    for k in ("base64", "bytesBase64", "contentBase64", "dataBase64"):
        v = holder.get(k)
        if isinstance(v, str) and v.strip():
            try:
                return base64.b64decode(v), mime
            except Exception:  # noqa: BLE001
                return None, None
    # Some builds return {content, encoding}
    content = holder.get("content") or holder.get("data") or holder.get("bytes")
    if isinstance(content, str):
        if str(holder.get("encoding") or "").lower() in ("base64", "b64"):
            try:
                return base64.b64decode(content), mime
            except Exception:  # noqa: BLE001
                return None, None
    if isinstance(content, (bytes, bytearray)):
        return bytes(content), mime
    return None, None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _sandbox_source(ref: str) -> str:
    if ref.startswith("file://"):
        return ref[len("file://"):]
    if ref.startswith("media://inbound/"):
        return ref[len("media://inbound/"):]
    if "__openclaw__/assistant-media" in ref:
        # ?source=<path>&mediaTicket=… — the path is what a file RPC wants.
        from urllib.parse import parse_qs
        q = parse_qs(urlsplit(ref).query)
        return (q.get("source") or [ref])[0]
    return ref


def _basename(url: str) -> str | None:
    path = urlsplit(url).path if "://" in url else url
    base = os.path.basename(path.rstrip("/"))
    return base or None


def _ext_for(mime: str | None, filename: str | None) -> str | None:
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext:
            return ext
    if mime:
        guess = mimetypes.guess_extension(mime.split(";")[0].strip())
        if guess:
            return ".jpg" if guess == ".jpe" else guess
    return None


def _kind_for(mime: str | None, url: str, filename: str | None) -> str:
    m = (mime or "").lower()
    if m.startswith("image/"):
        return "image"
    if m.startswith("video/"):
        return "video"
    if m.startswith("audio/"):
        return "audio"
    ext = _ext_for(None, filename) or _ext_for(None, _basename(url) or "") or ""
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "file"


def bound_attachments(refs: list[dict] | None, cap: int = 8) -> list[dict] | None:
    """Keep a persisted attachment list small and well-formed — for chat_store."""
    if not refs:
        return None
    out: list[dict] = []
    for r in refs[:cap]:
        if not isinstance(r, dict) or not r.get("url"):
            continue
        item = {"url": str(r["url"])[:2048], "kind": str(r.get("kind") or "file")}
        if r.get("filename"):
            item["filename"] = str(r["filename"])[:200]
        if r.get("mime"):
            item["mime"] = str(r["mime"])[:100]
        out.append(item)
    return out or None
