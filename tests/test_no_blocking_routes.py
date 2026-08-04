"""An `async def` route may not do blocking work on the event loop.

FastAPI runs a `def` route in a threadpool but an `async def` route directly on
the loop, so one blocking call inside an async route stalls the entire process —
every SSE stream, every /apps/{cid} proxy hop, and the login gate — for its full
duration. `/api/upload` ran `soffice --headless` (150s ceiling) that way, so a
single user uploading a single .docx could freeze the server. It never needed
load to bite; it bit at the second concurrent request.

The idiom is already known here: `run_in_threadpool` appears 40+ times in
phone_bridge.py. That is what makes the exceptions omissions rather than
ignorance, and what makes a static guard worth having — it catches the next one
at review time instead of in production.

Scope: only calls that are *directly* in an async route body count. A call inside
a helper the route awaits is out of scope for a static check; keep blocking work
in a named sync helper and hand it to run_in_threadpool, as `_store_upload` does.
"""
import ast
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _targets() -> list[str]:
    """Every tracked module that can define a route — DISCOVERED, not listed.

    This was a hardcoded three-entry list. Then phone_bridge.py was split into
    per-concern APIRouters, and each extraction silently moved routes OUT of the
    guard's scope: internal.py, learning_api.py, ops_api.py, chats_api.py and
    perf_api.py were all invisible to it while it kept reporting green. A guard
    with a fixed file list does not fail when code moves away from it — it just
    stops checking, which is the worst failure mode a guard has.

    So the list is derived instead. `git ls-files` matches the convention the
    rest of the repo's static guards already use (see tests/test_diagram_sync.py),
    and a new router module is covered the day it is committed, without anyone
    remembering to add it here.
    """
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py"],
                         capture_output=True, text=True, check=True).stdout
    keep = []
    for rel in out.splitlines():
        if not rel:
            continue
        top = rel.split("/", 1)[0]
        if top in {"tests", "qa", "overlay", "demo", "frontend", "tools", "sdk", "docs"}:
            continue
        if rel == "phone_bridge.py" or rel.startswith("ava_bridge/"):
            keep.append(rel)
    return sorted(keep)


TARGETS = _targets()

# Route decorators: @app.get(...), @router.post(...), etc.
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

# Calls that block, matched on the DOTTED name where there is a qualifier.
# Qualifying matters: `sleep` alone would flag `await asyncio.sleep()`, which is
# the correct non-blocking form, and `run` alone would flag `asyncio.run`.
# Deliberately curated — a broad net fires on harmless things and gets suppressed
# wholesale, which is how a guard dies.
_BLOCKING = {
    "subprocess.run": "subprocess.run",
    "subprocess.check_output": "subprocess.check_output",
    "subprocess.check_call": "subprocess.check_call",
    "subprocess.Popen": "subprocess.Popen",
    "time.sleep": "time.sleep",
    # Synchronous HTTP. `requests` has no async form, so a bare call here is
    # always a stall — and unlike the helpers below it is reached from routes in
    # several modules, not just the voice path.
    "requests.get": "requests.get",
    "requests.post": "requests.post",
    "requests.put": "requests.put",
    "requests.patch": "requests.patch",
    "requests.delete": "requests.delete",
    "va.transcribe": "CPU Whisper transcription",
}
# Bare names: project helpers whose blocking nature is not in doubt.
#
# The voice/turn seams were added after /api/talk was found running the whole
# turn on the loop: ffmpeg (30s), a speaker embedding, CPU Whisper and the agent
# turn (600s ceiling) — six blocking calls, all DIRECTLY in the route body, and
# all invisible here because the list only named the two document helpers. The
# guard reported green on the worst offender in the codebase.
#
# Deliberately NOT listed: settings.save_patch/save_config and the chat_store /
# memory_store calls. Those are small fsync'd writes and indexed reads that every
# hub POST already does on the loop; adding them would fire on ~13 accepted sites
# across 6 files and, per the note above, a guard that fires on accepted code is
# a guard that gets suppressed. Revisit if a store call ever grows a network hop.
_BLOCKING_BARE = {
    "extract_text": "document text extraction (may shell out to soffice)",
    "index_document": "SQLite FTS write",
    "decode_to_pcm": "ffmpeg subprocess (config.AUDIO_DECODE_TIMEOUT, 30s)",
    "gpu_transcribe": "HTTP request to the STT sidecar",
    "embed_pcm": "speaker-embedding model inference",
    "tts_wav_bytes": "speech synthesis",
    "_agent_run_turn": "the agent turn — config.OC_TIMEOUT, up to 600s",
}
# `open` is separately handled: reading a small config on the loop is fine, but
# writing an upload is not. Only flag it when the mode argument is a write mode.
_WRITE_MODES = {"w", "wb", "a", "ab", "w+", "wb+", "r+b"}

# file:line entries that are reviewed and accepted. Empty on purpose — add here
# only with a reason, never to silence the guard in bulk.
_ALLOW: set[str] = set()


def _is_route(node: ast.AsyncFunctionDef) -> bool:
    for d in node.decorator_list:
        call = d if isinstance(d, ast.Call) else None
        func = call.func if call else d
        if isinstance(func, ast.Attribute) and func.attr in _HTTP_METHODS:
            return True
    return False


def _dotted(f: ast.AST) -> str:
    """`subprocess.run` for an Attribute on a Name; else the bare trailing name."""
    if isinstance(f, ast.Attribute):
        if isinstance(f.value, ast.Name):
            return f"{f.value.id}.{f.attr}"
        return f.attr
    return getattr(f, "id", "")


def _walk_shallow(fn: ast.AST):
    """Like ast.walk, but does NOT descend into nested function bodies.

    A sync helper defined inside a route and handed to run_in_threadpool is the
    correct pattern, so its body must not be attributed to the route. Only what
    the route itself executes counts.
    """
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(n))


def _blocking_calls(fn: ast.AST) -> list[tuple[int, str]]:
    """(lineno, why) for blocking calls in this body.

    Two exclusions, both meaning "provably not on the loop":
      * handed to run_in_threadpool (its args are callables, not call nodes)
      * directly awaited — `await f()` means f is a coroutine by construction
    """
    skip: set[int] = set()
    for n in _walk_shallow(fn):
        if isinstance(n, ast.Call) and _dotted(n.func).split(".")[-1] == "run_in_threadpool":
            for a in n.args:
                skip.add(id(a))
        elif isinstance(n, ast.Await) and isinstance(n.value, ast.Call):
            skip.add(id(n.value))

    hits: list[tuple[int, str]] = []
    for n in _walk_shallow(fn):
        if not isinstance(n, ast.Call) or id(n) in skip:
            continue
        name = _dotted(n.func)
        bare = name.split(".")[-1]
        if name in _BLOCKING:
            hits.append((n.lineno, _BLOCKING[name]))
        elif bare in _BLOCKING_BARE:
            hits.append((n.lineno, _BLOCKING_BARE[bare]))
        elif name == "open":
            # Unqualified builtin only. `x.open(...)` is ambiguous — wave.open,
            # zipfile.open and Path.open all exist — and matching it produced
            # false hits on a debug-only wav dump. Narrow beats noisy: a guard
            # people trust is one that only fires on real problems.
            mode = n.args[1] if len(n.args) > 1 else None
            if isinstance(mode, ast.Constant) and mode.value in _WRITE_MODES:
                hits.append((n.lineno, f"open(..., {mode.value!r}) — blocking write"))
    return hits


def test_async_routes_do_no_blocking_work() -> None:
    offenders: list[str] = []
    for rel in TARGETS:
        path = ROOT / rel
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or not _is_route(node):
                continue
            for lineno, why in _blocking_calls(node):
                entry = f"{rel}:{lineno}"
                if entry in _ALLOW:
                    continue
                offenders.append(f"{entry}: {why}  (in async route {node.name}())")
    assert not offenders, (
        "blocking work inside an `async def` route stalls the whole event loop. "
        "Move it into a sync helper and `await run_in_threadpool(helper, ...)` — "
        "see _store_upload in ava_bridge/media_api.py:\n  " + "\n  ".join(sorted(offenders))
    )
