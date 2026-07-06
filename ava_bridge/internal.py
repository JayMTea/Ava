"""Internal capability endpoints — let Ava's sandboxed MCP tools read the text
of files the user uploaded to the bridge.

The MCP server runs inside the OpenClaw sandbox and can't see the host's upload
dir or extraction binaries, so document-reading is exposed HERE as a token-gated
host service it calls back into (the same host-callback pattern the image tool
uses for the GPU service). Text is extracted once at upload time and cached on the
attachment record; this re-extracts from disk only if that cache is empty.

Only callers presenting the shared X-Ava-Internal-Token (handed to the tools by
agent/install.sh) are served; everything else gets 401.
"""
import hashlib
import hmac
import os

from fastapi import Request

from . import config, documents, state

# Per-capability-group tokens handed to Ava's sandboxed MCP servers by
# agent/install.sh. Each server receives HMAC(root_secret, "ava-internal:<group>")
# so a low-risk tool token is distinct from the root secret. Must stay in sync
# with the `groups` list in agent/install.sh (both derive from the discovered
# mcp_server_<group> categories, core + optional overlay).
_TOKEN_GROUPS = ["admin", "content", "connectors",
                "productivity", "system", "wellness"]
try:  # optional private overlay contributes its own MCP-server capability groups
    from overlay.ava_bridge import personal_config as _personal_config
    _personal_config.extend_token_groups(_TOKEN_GROUPS)
except Exception:  # noqa: BLE001 - overlay is optional/gitignored
    pass
_TOKEN_GROUPS = tuple(_TOKEN_GROUPS)


def _derived_token(group: str) -> str:
    return hmac.new(config.INTERNAL_TOKEN.encode(),
                    f"ava-internal:{group}".encode(),
                    hashlib.sha256).hexdigest()


def _token_group(tok: str) -> str | None:
    """Return the group a presented token authorizes ('root' = full access), or
    None if it matches neither the root secret nor any derived group token."""
    if hmac.compare_digest(tok, config.INTERNAL_TOKEN):
        return "root"
    for group in _TOKEN_GROUPS:
        if hmac.compare_digest(tok, _derived_token(group)):
            return group
    return None


def authorized(request: Request, scope=None) -> bool:
    """True if the caller presents the root token or a valid derived group token.

    The MCP servers each hold a scoped token derived from the root secret, so the
    bridge accepts any of them (the root token always passes). When `scope` is
    given (a group name or iterable of names), a derived token is accepted only if
    its group is in that scope, backing least-privilege on sensitive routes.
    """
    if not config.INTERNAL_TOKEN:
        return False
    group = _token_group(request.headers.get("x-ava-internal-token", ""))
    if group is None:
        return False
    if group == "root" or scope is None:
        return True
    allowed = {scope} if isinstance(scope, str) else set(scope)
    return group in allowed


# ── Inbound "app → Ava" ingest tokens ──────────────────────────────────────
# A third-party device/sensor app is NOT a sandboxed MCP server, so it must not
# hold the internal token or reach the /internal/* tool surface. Instead each
# connector gets its own inbound token — derived from the root secret the same way
# as the MCP group tokens, but under a distinct 'ava-ingest:<cid>' namespace — that
# it presents (Authorization: Bearer <token>) to POST events to Ava. No new secret
# store; rotating the connector id rotates its token.
def ingest_token(cid: str) -> str:
    """The per-connector inbound bearer token an app presents to push events."""
    return hmac.new(config.INTERNAL_TOKEN.encode(),
                    f"ava-ingest:{cid}".encode(),
                    hashlib.sha256).hexdigest()


def verify_ingest(cid: str, presented: str) -> bool:
    """Constant-time check that `presented` is `cid`'s ingest token."""
    if not config.INTERNAL_TOKEN or not presented:
        return False
    return hmac.compare_digest(presented, ingest_token(cid))


def bearer(request: Request) -> str:
    """Extract a Bearer token from the Authorization header ('' if absent)."""
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


def documents_payload() -> dict:
    """List the documents/images the user has uploaded in this bridge session."""
    with state.attachments_lock:
        items = [
            {"id": r["id"], "filename": r["filename"], "kind": r["kind"],
             "chars": r.get("chars", 0), "has_text": bool(r.get("text"))}
            for r in state.attachments.values()
        ]
    return {"documents": items}


def extract_payload(file_id: str, max_chars: int) -> dict | None:
    """Return the extracted text for one uploaded file, or None if unknown."""
    with state.attachments_lock:
        rec = state.attachments.get(file_id)
        rec = dict(rec) if rec else None
    if not rec:
        return None
    text = (rec.get("text") or "").strip()
    name = rec["filename"]
    # Re-extract from disk if the cached text is empty (e.g. cleared on restart).
    if not text:
        upload_dir = os.path.realpath(config.UPLOAD_DIR)
        path = os.path.join(upload_dir, f'{rec["id"]}_{name}')
        ext = os.path.splitext(name)[1].lower()
        real = os.path.realpath(path)
        # Only read a real regular file that lives directly in the upload dir and
        # isn't a symlink — blocks symlink/traversal reads of files elsewhere.
        if (os.path.dirname(real) == upload_dir
                and not os.path.islink(path)
                and os.path.isfile(real)):
            text = (documents.extract_text(real, ext) or "").strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n\u2026[truncated]"
    return {"id": rec["id"], "filename": name, "kind": rec["kind"], "text": text}
