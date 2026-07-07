"""Code mode: Ava edits her own source with Claude (your Anthropic key).

Used only when the chat UI's Code-mode toggle is on. Runs HOST-side because the
repo lives on the host, not in the sandbox. It drives the Anthropic Messages API
with a small tool loop scoped to this repo (config.ROOT):

    list_dir / read_file / search   -> let Claude inspect the code
    write_file / delete_file        -> STAGE a change (never touches disk)

Nothing is written during a turn. Claude's edits are staged in-memory; the bridge
computes unified diffs and the UI shows them with Apply / Discard. Apply writes
the files and git-commits them (authored as Ava) so every change is one revert
away. UI/HTML changes need no restart; Python changes can opt into a detached
service restart.
"""
import difflib
import fnmatch
import os
import re
import subprocess
import threading
import time
import uuid

import httpx

from . import config, state

ROOT = os.path.realpath(config.ROOT)

# The code-change engine normally edits Ava's OWN repo (ROOT). To let it also
# operate on other whitelisted projects (registered by the optional overlay),
# the active repo root is held in a ContextVar so a single change-request can
# retarget every path/search/git helper below without threading `root` through
# each one. Defaults to ROOT, so Ava's self-edit path is byte-for-byte unchanged.
import contextvars
_active_root_var: contextvars.ContextVar[str] = contextvars.ContextVar("code_active_root", default=ROOT)


def _active_root() -> str:
    return _active_root_var.get()


def set_active_root(root: str):
    """Point the code tools at `root` for the current context; returns a token
    to pass to reset_active_root() when done."""
    return _active_root_var.set(os.path.realpath(root))


def reset_active_root(token) -> None:
    _active_root_var.reset(token)

# Directories that are never worth showing/editing (noise + secrets + binaries).
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "models", "bin",
              "media", "logs", "gpusvc", "data", "enroll"}

SYSTEM_PROMPT = (
    "You are Ava, editing your OWN source code in the repository rooted at "
    f"{ROOT}. The project is a FastAPI 'bridge' (Python, package `ava_bridge/`, "
    "entrypoint `phone_bridge.py`), a vanilla HTML/CSS/JS phone UI "
    "(`ava_bridge/web/index.html`), MCP tools under `agent/mcp_server/`, and a "
    "small inference router fronting the always-on open-model brain "
    "(`ava_router.py`).\n\n"
    "Work like a careful senior engineer:\n"
    "1. Use list_dir / read_file / search to understand the code BEFORE editing.\n"
    "2. Make the smallest focused change that satisfies the request.\n"
    "3. PREFER str_replace for edits: pass a unique `old` snippet (with enough "
    "surrounding context to match exactly once) and the `new` replacement. Use "
    "append_file to add lines at the end of a file. Only use write_file for NEW "
    "files or a full rewrite of a SMALL file \u2014 large files (e.g. index.html) "
    "cannot be re-emitted in one response, so never write_file them wholesale.\n"
    "4. Match the existing style; don't reformat unrelated code.\n"
    "5. When done, briefly explain what you changed and why, in your own voice.\n"
    "All paths are repo-relative (e.g. `ava_bridge/web/index.html`)."
)

TOOLS = [
    {"name": "list_dir",
     "description": "List the entries of a repo-relative directory.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "repo-relative dir, '' = root"}},
         "required": ["path"]}},
    {"name": "read_file",
     "description": "Read a repo-relative text file (returns current/staged content).",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
    {"name": "search",
     "description": "Regex search across repo text files. Optional glob filter.",
     "input_schema": {"type": "object", "properties": {
         "pattern": {"type": "string"},
         "glob": {"type": "string", "description": "e.g. '*.py' (optional)"}},
         "required": ["pattern"]}},
    {"name": "write_file",
     "description": "STAGE a full-file create/overwrite for human review. Use "
                    "ONLY for new files or a full rewrite of a SMALL file. For "
                    "edits to existing/large files use str_replace or append_file.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"},
         "content": {"type": "string", "description": "complete new file contents"}},
         "required": ["path", "content"]}},
    {"name": "str_replace",
     "description": "STAGE a targeted edit: replace an exact snippet with new text. "
                    "`old` must match EXACTLY ONCE in the current/staged file "
                    "(include enough surrounding context to be unique). Preferred "
                    "way to edit existing files without re-emitting the whole file.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"},
         "old": {"type": "string", "description": "exact text to replace (unique)"},
         "new": {"type": "string", "description": "replacement text"}},
         "required": ["path", "old", "new"]}},
    {"name": "append_file",
     "description": "STAGE appending text to the end of a file (creates it if "
                    "missing). Use to add lines without rewriting the file.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"},
         "text": {"type": "string", "description": "text to append at end of file"}},
         "required": ["path", "text"]}},
    {"name": "delete_file",
     "description": "STAGE a file deletion for human review.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
]


# --- path safety -------------------------------------------------------------
def _safe(rel: str) -> str:
    """Resolve a repo-relative path and refuse anything outside the repo."""
    root = _active_root()
    rel = (rel or "").strip().lstrip("/")
    p = os.path.realpath(os.path.join(root, rel))
    if p != root and not p.startswith(root + os.sep):
        raise ValueError(f"path escapes the repo: {rel!r}")
    return p


def _rel(p: str) -> str:
    return os.path.relpath(p, _active_root())


# --- tool implementations (staged-aware) -------------------------------------
def _tool_list_dir(rel: str) -> str:
    p = _safe(rel)
    if not os.path.isdir(p):
        return f"(not a directory: {rel})"
    out = []
    for name in sorted(os.listdir(p)):
        if name in _SKIP_DIRS:
            continue
        full = os.path.join(p, name)
        out.append(name + "/" if os.path.isdir(full) else name)
    return "\n".join(out) or "(empty)"


def _tool_read_file(rel: str, staged: dict) -> str:
    key = _rel(_safe(rel))
    if key in staged:
        st = staged[key]
        return "(staged deletion)" if st["new"] is None else st["new"]
    p = _safe(rel)
    if not os.path.isfile(p):
        return f"(no such file: {rel})"
    if os.path.getsize(p) > config.CODE_MAX_FILE_BYTES:
        return f"(file too large to read: {rel})"
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


def _tool_search(pattern: str, glob: str | None) -> str:
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"(bad regex: {e})"
    hits = []
    for dirpath, dirnames, filenames in os.walk(_active_root()):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if glob and not fnmatch.fnmatch(fn, glob):
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > config.CODE_MAX_FILE_BYTES:
                    continue
                with open(full, encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            hits.append(f"{_rel(full)}:{i}: {line.rstrip()[:200]}")
                            if len(hits) >= 80:
                                hits.append("\u2026(truncated)")
                                return "\n".join(hits)
            except (OSError, UnicodeError):
                continue
    return "\n".join(hits) or "(no matches)"


def _stage_write(rel: str, content: str, staged: dict) -> str:
    key = _rel(_safe(rel))
    staged[key] = {"new": content, "delete": False}
    return f"staged write to {key} ({len(content)} bytes) \u2014 pending your approval"


def _current_content(rel: str, staged: dict) -> tuple[str, bool]:
    """Return (content, exists) using staged content if present, else disk."""
    key = _rel(_safe(rel))
    if key in staged:
        st = staged[key]
        if st["new"] is None:
            return "", False
        return st["new"], True
    p = _safe(rel)
    if not os.path.isfile(p):
        return "", False
    if os.path.getsize(p) > config.CODE_MAX_FILE_BYTES:
        raise ValueError(f"file too large to edit: {rel}")
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read(), True


def _stage_str_replace(rel: str, old: str, new: str, staged: dict) -> str:
    key = _rel(_safe(rel))
    content, exists = _current_content(rel, staged)
    if not exists:
        raise ValueError(f"no such file: {rel} (use write_file to create it)")
    if old == "":
        raise ValueError("`old` must not be empty")
    count = content.count(old)
    if count == 0:
        raise ValueError(f"`old` snippet not found in {rel} \u2014 re-read the file "
                         "and copy the exact text (including whitespace)")
    if count > 1:
        raise ValueError(f"`old` snippet is not unique in {rel} ({count} matches) "
                         "\u2014 include more surrounding context")
    staged[key] = {"new": content.replace(old, new, 1), "delete": False}
    return f"staged edit to {key} (1 replacement) \u2014 pending your approval"


def _stage_append(rel: str, text: str, staged: dict) -> str:
    key = _rel(_safe(rel))
    content, exists = _current_content(rel, staged)
    if exists and content and not content.endswith("\n"):
        content += "\n"
    staged[key] = {"new": content + text, "delete": False}
    verb = "append to" if exists else "create"
    return f"staged {verb} {key} (+{len(text)} bytes) \u2014 pending your approval"


def _stage_delete(rel: str, staged: dict) -> str:
    key = _rel(_safe(rel))
    staged[key] = {"new": None, "delete": True}
    return f"staged deletion of {key} \u2014 pending your approval"


# --- diffs -------------------------------------------------------------------
def _edits_from_staged(staged: dict) -> list[dict]:
    edits = []
    root = _active_root()
    for key, st in staged.items():
        p = os.path.join(root, key)
        old = ""
        existed = os.path.isfile(p)
        if existed:
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    old = f.read()
            except OSError:
                old = ""
        if st["new"] is None:  # deletion
            status, new = "deleted", ""
        else:
            new = st["new"]
            status = "modified" if existed else "new"
        diff = "".join(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a/{key}", tofile=f"b/{key}"))
        added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
        edits.append({"path": key, "status": status, "diff": diff, "new": new,
                      "added": added, "removed": removed})
    return edits


# --- Anthropic call ----------------------------------------------------------
def _anthropic(messages: list, model: str, system: str | None = None) -> dict:
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (add it to .env)")
    r = httpx.post(
        f"{config.ANTHROPIC_BASE}/v1/messages",
        headers={"x-api-key": config.ANTHROPIC_API_KEY,
                 "anthropic-version": config.ANTHROPIC_VERSION,
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": config.CODE_MAX_TOKENS,
              "system": system or SYSTEM_PROMPT, "tools": TOOLS, "messages": messages},
        timeout=180.0)
    if r.status_code >= 400:
        raise RuntimeError(f"Anthropic {r.status_code}: {r.text[:300]}")
    return r.json()


def list_models() -> list[str]:
    """Claude model ids for the UI dropdown (live, with a static fallback)."""
    if config.ANTHROPIC_API_KEY:
        try:
            r = httpx.get(f"{config.ANTHROPIC_BASE}/v1/models",
                          headers={"x-api-key": config.ANTHROPIC_API_KEY,
                                   "anthropic-version": config.ANTHROPIC_VERSION},
                          timeout=10.0)
            if r.status_code < 400:
                ids = [m["id"] for m in (r.json().get("data") or []) if m.get("id")]
                if ids:
                    return ids
        except Exception:  # noqa: BLE001 — fall back to the static list
            pass
    return list(config.CODE_MODELS_FALLBACK)


# --- turn loop ---------------------------------------------------------------
def _set(tid: str, **kw):
    with state.code_turns_lock:
        if tid in state.code_turns:
            state.code_turns[tid].update(**kw)


def _push_step(tid: str, step: dict):
    with state.code_turns_lock:
        if tid in state.code_turns:
            state.code_turns[tid].setdefault("steps", []).append(step)


def _apply_inline_fix(error_msg: str, context: dict) -> tuple[bool, str, bool]:
    """Attempt to auto-fix common errors during a turn.
    
    Returns (fixed, description, critical) where:
      fixed=True if fix was applied automatically
      description=explanation of what happened
      critical=True if this error should be flagged for manual review instead
    
    Critical errors (permission denied, path escapes, etc.) are logged but not auto-fixed.
    """
    from . import learning
    
    error_lower = error_msg.lower()
    
    # Permission/access error: flag as critical (needs human review)
    if "permission" in error_lower or "access" in error_lower or "denied" in error_lower:
        description = "Checked file permissions — flagged for manual review"
        learning.code_learner.record_inline_fix(error_msg, description, False, critical=True)
        return False, description, True
    
    # File too large: reduce batch size for search/read
    if "file" in error_lower and ("large" in error_lower or "size" in error_lower):
        context["reduce_file_size"] = True
        description = "Reduced file read batch size for large files"
        learning.code_learner.record_inline_fix(error_msg, description, True, critical=False)
        return True, description, False
    
    # Timeout on operation: add caching hint
    if "timeout" in error_lower or "timed out" in error_lower:
        context["add_caching"] = True
        description = "Added caching hint for slow queries"
        learning.code_learner.record_inline_fix(error_msg, description, True, critical=False)
        return True, description, False
    
    # Search pattern error: simplify regex
    if "search" in error_lower and ("pattern" in error_lower or "regex" in error_lower):
        context["simplify_search"] = True
        description = "Simplified search pattern for robustness"
        learning.code_learner.record_inline_fix(error_msg, description, True, critical=False)
        return True, description, False
    
    # Unknown error: log it for learning
    learning.code_learner.record_inline_fix(error_msg, "Unknown error type", False, critical=False)
    return False, "", False



def _run_code_turn(tid: str, text: str, model: str):
    staged: dict = {}
    messages = [{"role": "user", "content": text}]
    inline_fixes_applied = 0
    try:
        for _ in range(config.CODE_MAX_ITERS):
            data = _anthropic(messages, model)
            blocks = data.get("content") or []
            messages.append({"role": "assistant", "content": blocks})
            tool_results = []
            final_text = []
            for b in blocks:
                bt = b.get("type")
                if bt == "text":
                    tx = (b.get("text") or "").strip()
                    if tx:
                        final_text.append(tx)
                        _push_step(tid, {"kind": "text", "text": tx})
                elif bt == "tool_use":
                    name, inp, tuid = b.get("name"), b.get("input") or {}, b.get("id")
                    label = name + (f" {inp.get('path')}" if inp.get("path") else "")
                    _push_step(tid, {"kind": "tool", "name": label})
                    res = None
                    retry_context = {}
                    for attempt in range(2):  # Try once, then retry once if inline fix applied
                        try:
                            if name == "list_dir":
                                res = _tool_list_dir(inp.get("path", ""))
                            elif name == "read_file":
                                res = _tool_read_file(inp.get("path", ""), staged)
                            elif name == "search":
                                res = _tool_search(inp.get("pattern", ""), inp.get("glob"))
                            elif name == "write_file":
                                res = _stage_write(inp.get("path", ""), inp.get("content", ""), staged)
                            elif name == "str_replace":
                                res = _stage_str_replace(inp.get("path", ""), inp.get("old", ""), inp.get("new", ""), staged)
                            elif name == "append_file":
                                res = _stage_append(inp.get("path", ""), inp.get("text", ""), staged)
                            elif name == "delete_file":
                                res = _stage_delete(inp.get("path", ""), staged)
                            else:
                                res = f"(unknown tool: {name})"
                            break  # Success, don't retry
                        except Exception as e:  # noqa: BLE001 — report to the model
                            error_msg = str(e)
                            if attempt == 0:
                                # First attempt failed; try inline fix
                                fixed, fix_desc, critical = _apply_inline_fix(error_msg, retry_context)
                                if critical:
                                    # Critical error: don't retry, just report to Claude
                                    res = f"CRITICAL ERROR (requires approval): {error_msg}"
                                    break
                                elif fixed:
                                    inline_fixes_applied += 1
                                    _push_step(tid, {
                                        "kind": "text",
                                        "text": f"[auto-fixed: {fix_desc}]"
                                    })
                                    continue  # Retry the tool call
                            # Retry exhausted or no fix available
                            res = f"ERROR: {error_msg}"
                            break
                    tool_results.append({"type": "tool_result", "tool_use_id": tuid,
                                         "content": res[:60000]})
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
                continue
            # No tool calls -> Claude is done.
            edits = _edits_from_staged(staged)
            reply = "\n\n".join(final_text).strip() or (
                "Done." if edits else "I didn't make any changes.")
            if inline_fixes_applied:
                reply = f"[Applied {inline_fixes_applied} inline fix(es) during execution]\n\n{reply}"
            _set(tid, status="done", reply=reply, edits=edits)
            return
        # Hit the iteration cap.
        _set(tid, status="done", reply="(stopped: hit the step limit)",
             edits=_edits_from_staged(staged))
    except Exception as e:  # noqa: BLE001
        _set(tid, status="error", error=str(e))


def start_code_turn(text: str, model: str) -> str:
    tid = uuid.uuid4().hex[:12]
    with state.code_turns_lock:
        state.code_turns[tid] = {"id": tid, "status": "running", "steps": [],
                                 "reply": None, "edits": [], "model": model,
                                 "applied": False, "error": None,
                                 "created": time.time()}
    threading.Thread(target=_run_code_turn, args=(tid, text, model), daemon=True).start()
    return tid


# --- apply -------------------------------------------------------------------
def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", _active_root(), *args], capture_output=True, text=True)


def apply_turn(tid: str, restart: bool = False) -> dict:
    with state.code_turns_lock:
        t = state.code_turns.get(tid)
        if not t:
            return {"ok": False, "error": "unknown turn"}
        if t.get("applied"):
            return {"ok": False, "error": "already applied"}
        edits = list(t.get("edits") or [])
        model = t.get("model") or "claude"
    if not edits:
        return {"ok": False, "error": "no changes to apply"}

    changed: list[str] = []
    for e in edits:
        key = e["path"]
        p = _safe(key)
        if e["status"] == "deleted":
            if os.path.isfile(p):
                os.remove(p)
            changed.append(key)
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(e.get("new") or "")
            changed.append(key)

    # Targeted commit, authored as Ava, scoped to just these files.
    _git(["add", "--", *changed])
    msg = f"Ava (code mode): {edits[0]['path']}" + (
        f" +{len(edits)-1} more" if len(edits) > 1 else "")
    cp = _git(["-c", "user.name=Ava", "-c", "user.email=ava@localhost",
               "commit", "-m", msg, "--", *changed])
    commit = ""
    if cp.returncode == 0:
        commit = (_git(["rev-parse", "--short", "HEAD"]).stdout or "").strip()

    with state.code_turns_lock:
        if tid in state.code_turns:
            state.code_turns[tid]["applied"] = True

    if restart:
        # Detached so the response flushes before the bridge restarts itself.
        subprocess.Popen(["bash", "-lc",
                          "sleep 1; systemctl --user restart ava-bridge.service"],
                         start_new_session=True)

    return {"ok": True, "files": changed, "commit": commit, "restart": restart,
            "commit_failed": cp.returncode != 0,
            "commit_error": (cp.stderr or "").strip() if cp.returncode != 0 else ""}


def discard_turn(tid: str) -> dict:
    with state.code_turns_lock:
        t = state.code_turns.get(tid)
        if not t:
            return {"ok": False, "error": "unknown turn"}
        t["edits"] = []
        t["discarded"] = True
    return {"ok": True}
