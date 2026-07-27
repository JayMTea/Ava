"""Autonomous code-change agent — Ava fixes/changes her own source via Claude,
gated by the access policy.

This is the engine behind the `code_change_request` MCP tool (and Ava's own
self-recursive error fixing). Unlike `coder.py` (which STAGES every edit for the
Code-mode UI), this runs the full loop end-to-end and then DECIDES per file:

    auto     -> write it and git-commit immediately (Ava's own voice)
    approval -> park it as a pending proposal on the Learning page for the owner
    denied   -> refuse (secrets / biometrics / git internals)

So Ava can refactor a tool, fix a UI bug, or repair an error she hit — all on her
own — but she can never silently change auth, egress policy, or the deploy scripts
without the owner clicking Approve. See access_policy.py for the tiers.
"""
import os
import subprocess
import uuid
from datetime import datetime, timezone

from . import audit, config, state, access_policy
from .coder import (
    SYSTEM_PROMPT, _safe, _git,
    _tool_list_dir, _tool_read_file, _tool_search,
    _stage_write, _stage_str_replace, _stage_append, _stage_delete,
    _edits_from_staged, _anthropic, set_active_root, reset_active_root,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- project resolution (Ava's own repo vs whitelisted sibling projects) -----
def _project_cfg(project: str) -> dict:
    """Return the config.PROJECTS entry, defaulting to Ava's own repo."""
    return config.PROJECTS.get(project) or config.PROJECTS["ava"]


def _system_for(project: str, root: str) -> str:
    """The Claude system prompt for the target repo. Ava's own repo keeps the
    original prompt; other projects get a generic careful-engineer prompt."""
    if project == "ava":
        return SYSTEM_PROMPT
    label = _project_cfg(project).get("label", project)
    return (
        f"You are {config.AVA_NAME}, making a code change to the '{label}' project, a separate "
        f"repository rooted at {root}. You do NOT own this repo, so be extra "
        "careful and conservative.\n\n"
        "Work like a careful senior engineer:\n"
        "1. Use list_dir / read_file / search to understand the code BEFORE editing.\n"
        "2. Make the smallest focused change that satisfies the request.\n"
        "3. PREFER str_replace: pass a unique `old` snippet (with enough surrounding "
        "context to match exactly once) and the `new` replacement. Use append_file "
        "to add lines at the end; only use write_file for NEW or small files.\n"
        "4. Match the existing style; do not reformat unrelated code.\n"
        "5. Do NOT touch secrets, credentials, deploy scripts, databases or config "
        "files — those are blocked and will be rejected.\n"
        "6. When done, briefly explain what you changed and why.\n"
        "All paths are repo-relative."
    )


# --- run the Claude tool loop to completion, returning staged edits ----------
def _run_loop(request: str, context: str, files: list[str], model: str,
              system: str | None = None) -> tuple[list[dict], str, list[str]]:
    """Drive Claude until it stops calling tools. Returns (edits, reply, trace)."""
    prompt = request.strip()
    if context:
        prompt += f"\n\nAdditional context:\n{context.strip()}"
    if files:
        prompt += "\n\nFocus on these files:\n" + "\n".join(f"  - {f}" for f in files)

    staged: dict = {}
    trace: list[str] = []
    messages = [{"role": "user", "content": prompt}]
    final_text: list[str] = []

    for _ in range(config.CODE_MAX_ITERS):
        data = _anthropic(messages, model, system=system)
        blocks = data.get("content") or []
        messages.append({"role": "assistant", "content": blocks})
        tool_results = []
        for b in blocks:
            bt = b.get("type")
            if bt == "text":
                tx = (b.get("text") or "").strip()
                if tx:
                    final_text.append(tx)
            elif bt == "tool_use":
                name, inp, tuid = b.get("name"), b.get("input") or {}, b.get("id")
                trace.append(name + (f" {inp.get('path')}" if inp.get("path") else ""))
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
                except Exception as e:  # noqa: BLE001 — report to the model so it can adapt (self-fix)
                    res = f"ERROR: {e}"
                tool_results.append({"type": "tool_result", "tool_use_id": tuid,
                                     "content": str(res)[:60000]})
        if tool_results:
            messages.append({"role": "user", "content": tool_results})
            continue
        break  # Claude stopped calling tools -> done

    reply = "\n\n".join(final_text).strip() or "Done."
    return _edits_from_staged(staged), reply, trace


# --- apply a set of edits to disk + git-commit -------------------------------
def _apply_edits(edits: list[dict], message: str, restart: bool = False) -> dict:
    changed: list[str] = []
    for e in edits:
        key = e["path"]
        p = _safe(key)
        if e["status"] == "deleted":
            if os.path.isfile(p):
                os.remove(p)
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(e.get("new") or "")
        changed.append(key)

    _git(["add", "--", *changed])
    cp = _git(["-c", "user.name=Ava", "-c", "user.email=ava@localhost",
               "commit", "-m", message, "--", *changed])
    commit = ""
    if cp.returncode == 0:
        commit = (_git(["rev-parse", "--short", "HEAD"]).stdout or "").strip()

    if restart:
        subprocess.Popen(["bash", "-lc",
                          "sleep 1; systemctl --user restart ava-bridge.service"],
                         start_new_session=True)
    return {"files": changed, "commit": commit,
            "commit_failed": cp.returncode != 0,
            "commit_error": (cp.stderr or "").strip() if cp.returncode != 0 else ""}


def _needs_bridge_restart(edits: list[dict]) -> bool:
    """Autonomous runtime-code edits should take effect immediately."""
    runtime_suffixes = (".py", ".sh")
    runtime_roots = ("ava_bridge/", "agent/mcp_server", "agent/install.sh")
    for edit in edits:
        path = edit.get("path", "")
        if path == "phone_bridge.py" or path.endswith(runtime_suffixes):
            return True
        if any(path.startswith(root) for root in runtime_roots):
            return True
    return False


# --- park approval-required edits into the Learning bucket -------------------
def _park_for_approval(edits: list[dict], request: str, reply: str,
                       source: str, project: str = "ava", actor: str = "Ava") -> dict:
    """Create a pending proposal (with staged changes) on the Learning page."""
    paths = [e["path"] for e in edits]
    added = sum(e.get("added", 0) for e in edits)
    removed = sum(e.get("removed", 0) for e in edits)
    label = _project_cfg(project).get("label", project)
    if project == "ava":
        if config.CODE_APPROVAL == "all":
            why = ("code.approval is 'all' — every code change waits for your "
                   "approval before it is applied.")
        else:
            why = ("Touches a protected file (auth / config / policy / deploy) — "
                   "needs your approval before it is applied.")
    else:
        why = (f"Change to the external '{label}' project — every edit there needs "
               "your approval, lands on a review branch (never the mainline), and "
               "must pass the project's tests before it applies.")
    proposal = {
        "id": f"prop_{uuid.uuid4().hex[:8]}",
        "title": (request.strip()[:80] or "Code change") ,
        "description": reply[:600],
        "why": why,
        "risk": "high",
        "effort": "minutes",
        "type": "code_change",
        "status": "pending",
        "requires_approval": True,
        "source": source,                 # 'code_change_request' | 'self_fix'
        "requested_by": actor,            # who asked for the change
        "completed_by": None,             # set on apply (approver or Ava)
        "completed_at": None,
        "applied": False,
        "project": project,               # which repo these edits target
        "paths": paths,
        "added": added,
        "removed": removed,
        "staged_changes": edits,          # full edit dicts (path/status/new/diff)
        "created": _now_iso(),
    }
    cycle = {
        "id": f"cycle_code_{uuid.uuid4().hex[:8]}",
        "timestamp": _now_iso(),
        "context": "code",
        "patterns": {"source": source, "project": project,
                     "request": request.strip()[:200], "files": paths},
        "proposals": [proposal],
    }
    with state.code_learning_state_lock:
        state.code_learning_state.setdefault("cycles", []).insert(0, cycle)
        state.code_learning_state["cycles"] = state.code_learning_state["cycles"][:20]
        state.code_learning_state["last_cycle"] = cycle["timestamp"]
    state.save_learning_state()
    audit.record("code_change", outcome="parked_for_approval", source=source,
                 project=project, actor=actor, paths=paths,
                 request=request.strip()[:200], proposal_id=proposal["id"])
    return proposal


# --- record an autonomous (auto-applied) change as a completed proposal ------
def _record_completed(edits: list[dict], request: str, reply: str, source: str,
                      project: str, commit: str, actor: str = "Ava") -> dict:
    """Log an auto-applied change on the Learning page as a completed record so
    the owner can see what Ava changed on her own — no action needed, no approve/reject."""
    now = _now_iso()
    paths = [e["path"] for e in edits]
    proposal = {
        "id": f"prop_{uuid.uuid4().hex[:8]}",
        "title": (request.strip()[:80] or "Code change"),
        "description": reply[:600],
        "why": "",
        "risk": "low",
        "effort": "minutes",
        "type": "code_change",
        "status": "completed",
        "requires_approval": False,
        "source": source,
        "requested_by": actor,
        "completed_by": actor,             # applied autonomously — no human gate
        "completed_at": now,
        "applied": True,
        "applied_commit": commit,
        "project": project,
        "paths": paths,
        "added": sum(e.get("added", 0) for e in edits),
        "removed": sum(e.get("removed", 0) for e in edits),
        "staged_changes": edits,
        "created": now,
    }
    cycle = {
        "id": f"cycle_code_{uuid.uuid4().hex[:8]}",
        "timestamp": now,
        "context": "code",
        "patterns": {"source": source, "project": project, "auto_applied": True,
                     "request": request.strip()[:200], "files": paths},
        "proposals": [proposal],
    }
    with state.code_learning_state_lock:
        state.code_learning_state.setdefault("cycles", []).insert(0, cycle)
        state.code_learning_state["cycles"] = state.code_learning_state["cycles"][:20]
        state.code_learning_state["last_cycle"] = cycle["timestamp"]
    state.save_learning_state()
    # Durable audit record — the Learning list keeps only 20 cycles, the ledger
    # keeps this forever (auto-applied edits aren't otherwise recoverable).
    audit.record("code_change", outcome="auto_applied", source=source,
                 project=project, commit=commit, actor=actor, paths=paths,
                 request=request.strip()[:200])
    return proposal


# --- public entry: run a code-change request end-to-end ----------------------
def run_change_request(request: str, context: str = "", files: list[str] | None = None,
                       source: str = "code_change_request",
                       model: str | None = None, project: str = "ava",
                       actor: str = "Ava") -> dict:
    """Fulfil a code-change request. Auto-applies safe edits, routes protected
    ones to the owner's approval bucket, refuses denied paths. Returns a summary dict
    shaped for the `code_change_request` MCP tool.

    `project` selects the target repo. "ava" (default) edits Ava's own source
    with the usual auto/approval/denied tiers. Other whitelisted projects are
    `approval_only`: every non-denied edit is parked for the owner.
    """
    if not config.ANTHROPIC_API_KEY:
        return {"status": "error",
                "errors": ["ANTHROPIC_API_KEY is not set (add it to .env)"],
                "summary": "", "files_changed": []}
    if project not in config.PROJECTS:
        return {"status": "error",
                "errors": [f"unknown project '{project}' (allowed: {', '.join(config.PROJECTS)})"],
                "summary": "", "files_changed": []}
    model = model or config.CODE_MODEL
    files = files or []
    pcfg = _project_cfg(project)
    root = pcfg["root"]
    if not os.path.isdir(root):
        return {"status": "error", "errors": [f"project root not found: {root}"],
                "summary": "", "files_changed": []}

    token = set_active_root(root)
    try:
        edits, reply, _trace = _run_loop(
            request, context, files, model, system=_system_for(project, root))
    except Exception as e:  # noqa: BLE001
        reset_active_root(token)
        return {"status": "error", "errors": [str(e)],
                "summary": "", "files_changed": []}

    if not edits:
        reset_active_root(token)
        return {"status": "no_changes", "summary": reply,
                "files_changed": [], "errors": []}

    buckets = access_policy.classify_edits(edits, project)

    # Honor code.approval for edits to Ava's OWN repo. Denied paths are never
    # promoted; external projects are always approval-only (classify_edits
    # already routes them), so this only re-buckets project == "ava".
    if project == "ava":
        if config.CODE_APPROVAL == "all":       # safe fork default: gate everything
            buckets["approval"] = buckets["auto"] + buckets["approval"]
            buckets["auto"] = []
        elif config.CODE_APPROVAL == "none":     # trusted box: auto-apply non-denied
            buckets["auto"] = buckets["auto"] + buckets["approval"]
            buckets["approval"] = []

    result = {"summary": reply, "files_changed": [], "errors": [],
              "git_commit": None, "pending_approval": [], "project": project}

    # 1) refuse denied paths outright
    for e in buckets["denied"]:
        result["errors"].append(f"blocked (protected secret/binary): {e['path']}")

    # 2) auto-apply the safe edits + commit (never happens for approval_only projects)
    if buckets["auto"]:
        msg = f"Ava ({source}): " + buckets["auto"][0]["path"] + (
            f" +{len(buckets['auto'])-1} more" if len(buckets["auto"]) > 1 else "")
        applied = _apply_edits(buckets["auto"], msg,
                               restart=(project == "ava" and _needs_bridge_restart(buckets["auto"])))
        for e in buckets["auto"]:
            result["files_changed"].append(
                {"path": e["path"], "description": f"{e['status']} (+{e.get('added',0)}/-{e.get('removed',0)})"})
        result["git_commit"] = applied.get("commit")
        if applied.get("commit_failed"):
            result["errors"].append(f"git commit failed: {applied.get('commit_error')}")
        else:
            # Log the autonomous change as a completed record (visible on the
            # Learning page, attributed to whoever drove it — Ava by default).
            _record_completed(buckets["auto"], request, reply, source, project,
                              applied.get("commit", ""), actor)
            result["completed_by"] = actor

    # 3) park protected edits for the owner's approval
    if buckets["approval"]:
        prop = _park_for_approval(buckets["approval"], request, reply, source, project, actor)
        for e in buckets["approval"]:
            result["pending_approval"].append(
                {"path": e["path"], "description": f"{e['status']} — awaiting approval"})
        result["proposal_id"] = prop["id"]

    # status roll-up
    if buckets["approval"] and not buckets["auto"]:
        result["status"] = "pending_approval"
    elif buckets["approval"] and buckets["auto"]:
        result["status"] = "partial"
    elif buckets["denied"] and not buckets["auto"]:
        result["status"] = "blocked"
    else:
        result["status"] = "completed"
    reset_active_root(token)
    return result


# --- run a project's test suite before an external edit is allowed to land ---
def _run_tests(root: str, cmd: list[str], timeout: int) -> tuple[bool, str]:
    """Run the configured test command in `root`. Returns (passed, tail_output)."""
    if not cmd:
        return True, "(no test command configured)"
    try:
        cp = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"tests timed out after {timeout}s"
    except FileNotFoundError as e:
        return False, f"test runner not found: {e}"
    out = ((cp.stdout or "") + "\n" + (cp.stderr or "")).strip()
    return cp.returncode == 0, out[-1500:]


# --- apply an approved external-project edit on a review branch (test-gated) --
def _apply_on_branch(safe: list[dict], pcfg: dict, title: str, proposal_id: str) -> dict:
    """For non-Ava projects: land the edit on ava/proposed-<id> (never the
    mainline), but only after the project's tests pass. The working tree is
    returned to its original branch afterward, so nothing runs the change until
    the owner merges the branch."""
    root = pcfg["root"]
    if _git(["rev-parse", "--is-inside-work-tree"]).returncode != 0:
        return {"ok": False, "error": f"{root} is not a git repository"}

    orig = (_git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout or "").strip() or "HEAD"
    branch = f"ava/proposed-{proposal_id}"

    co = _git(["checkout", "-B", branch])
    if co.returncode != 0:
        return {"ok": False, "error": f"could not create branch {branch}: {(co.stderr or '').strip()}"}

    # Snapshot the exact prior on-disk state of ONLY the files we're about to
    # touch, so a failed gate reverts our edits surgically without disturbing any
    # unrelated uncommitted work already in the project's working tree.
    prior: dict[str, str | None] = {}
    for e in safe:
        p = _safe(e["path"])
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    prior[e["path"]] = f.read()
            except OSError:
                prior[e["path"]] = None
        else:
            prior[e["path"]] = None  # didn't exist

    def _restore():
        for rel, content in prior.items():
            p = _safe(rel)
            if content is None:
                if os.path.isfile(p):
                    os.remove(p)
            else:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(content)
        _git(["checkout", orig])
        _git(["branch", "-D", branch])  # nothing landed — drop the empty branch

    # Write the files onto the branch working tree (no commit yet).
    changed: list[str] = []
    for e in safe:
        p = _safe(e["path"])
        if e["status"] == "deleted":
            if os.path.isfile(p):
                os.remove(p)
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(e.get("new") or "")
        changed.append(e["path"])

    # Gate 1 — baseline: every changed .py must byte-compile with the project's
    # own interpreter (catches syntax errors the edit introduced). Always runs.
    py_files = [os.path.join(root, c) for c in changed
                if c.endswith(".py") and any(e["path"] == c and e["status"] != "deleted" for e in safe)]
    interp = pcfg.get("python")
    if py_files and interp and os.path.exists(interp):
        try:
            cc = subprocess.run([interp, "-m", "py_compile", *py_files],
                                cwd=root, capture_output=True, text=True, timeout=120)
        except Exception as ex:  # noqa: BLE001
            _restore()
            return {"ok": False, "error": f"compile-check could not run: {ex}",
                    "branch": branch, "reverted": True}
        if cc.returncode != 0:
            _restore()
            return {"ok": False, "error": "compile-check failed — change not applied",
                    "test_output": (cc.stderr or cc.stdout or "")[-1500:],
                    "branch": branch, "reverted": True}

    # Gate 2 — optional: the project's own test suite (if configured).
    passed, tail = _run_tests(root, pcfg.get("test_cmd") or [], pcfg.get("test_timeout", 600))
    if not passed:
        _restore()
        return {"ok": False, "error": "tests failed — change not applied", "test_output": tail,
                "branch": branch, "reverted": True}

    _git(["add", "--", *changed])
    cp = _git(["-c", "user.name=Ava", "-c", "user.email=ava@localhost",
               "commit", "-m", f"Ava (approved): {title[:72]}", "--", *changed])
    commit = ""
    if cp.returncode == 0:
        commit = (_git(["rev-parse", "--short", "HEAD"]).stdout or "").strip()
    # Return the working tree to the original branch; the change stays on `branch`.
    _git(["checkout", orig])
    return {"ok": cp.returncode == 0, "files": changed, "commit": commit,
            "branch": branch, "restart": False, "tests_passed": True,
            "commit_failed": cp.returncode != 0,
            "error": "" if cp.returncode == 0 else (cp.stderr or "").strip()}


# --- apply a proposal the owner approved on the Learning page ----------------
def apply_approved_proposal(proposal_id: str) -> dict:
    """Write + commit the staged changes stored on an approved code_change proposal."""
    target = None
    with state.code_learning_state_lock:
        for cycle in state.code_learning_state.get("cycles", []):
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    target = prop
                    break
            if target:
                break
        if not target:
            return {"ok": False, "error": "proposal not found"}
        if target.get("applied"):
            return {"ok": False, "error": "already applied"}
        edits = list(target.get("staged_changes") or [])
        title = target.get("title") or "code change"
        project = target.get("project") or "ava"
        # The change is EXECUTED by whoever initiated it (Ava's autonomous edit,
        # or "Claude Code" when the user asked the assistant to do it). Approving only
        # unlocks it — the operator is recorded separately as approved_by.
        actor = target.get("requested_by") or "Ava"

    if not edits:
        return {"ok": False, "error": "no staged changes on this proposal"}
    if project not in config.PROJECTS:
        return {"ok": False, "error": f"unknown project '{project}'"}

    pcfg = _project_cfg(project)
    token = set_active_root(pcfg["root"])
    try:
        # Re-check policy at apply time — never let a denied path through even if
        # it somehow got parked.
        safe = [e for e in edits if access_policy.classify(e["path"], project) != "denied"]
        blocked = [e["path"] for e in edits if access_policy.classify(e["path"], project) == "denied"]

        # External projects: land on a test-gated review branch.
        if pcfg.get("branch"):
            res = _apply_on_branch(safe, pcfg, title, proposal_id)
            if res.get("ok"):
                with state.code_learning_state_lock:
                    target["status"] = "completed"
                    target["applied"] = True
                    target["applied_at"] = _now_iso()
                    target["completed_by"] = actor
                    target["approved_by"] = config.OPERATOR_NAME
                    target["completed_at"] = _now_iso()
                    target["applied_commit"] = res.get("commit")
                    target["applied_branch"] = res.get("branch")
                state.save_learning_state()
            res["blocked"] = blocked
            return res

        # Ava's own repo: apply + commit in place, restarting for Python changes.
        needs_restart = any(e["path"].endswith(".py") for e in safe)
        applied = _apply_edits(safe, f"Ava (approved): {title[:72]}", restart=needs_restart)
    finally:
        reset_active_root(token)

    with state.code_learning_state_lock:
        target["status"] = "completed"
        target["applied"] = True
        target["applied_at"] = _now_iso()
        target["completed_by"] = actor
        target["approved_by"] = config.OPERATOR_NAME
        target["completed_at"] = _now_iso()
        target["applied_commit"] = applied.get("commit")
    state.save_learning_state()
    audit.record("code_change", outcome="approved", project=project,
                 commit=applied.get("commit"), actor=actor,
                 approved_by=config.OPERATOR_NAME,
                 paths=[e["path"] for e in safe], blocked=blocked)

    return {"ok": True, "files": applied.get("files", []),
            "commit": applied.get("commit"), "restart": needs_restart,
            "blocked": blocked,
            "commit_failed": applied.get("commit_failed", False)}
