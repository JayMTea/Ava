---
name: "ava-self-coding"
icon: code
description: "How Ava changes and fixes her OWN source code by handing the task to Claude via her code_change_request tool. Use whenever the user asks Ava to change, add, fix, refactor, or improve something in her app/backend/UI/tools, OR when Ava notices a bug, error, traceback, or broken behavior in herself and wants to repair it. Claude edits the repo directly with the owner's ANTHROPIC_API_KEY: safe files apply automatically; protected files (auth, config, egress policy, deploy scripts) go to the user's approval bucket on the Learning page; secrets and biometrics are never touched. Trigger keywords - change your code, fix yourself, fix the bug, update the app, edit the UI, add a feature, refactor, you have an error, self-fix, modify the backend, change how you work."
---

# Ava Edits & Fixes Her Own Code (via Claude)

Ava can modify her own source. She does NOT edit files herself — she hands the
engineering task to **Claude** (using the owner's `ANTHROPIC_API_KEY`) through the
`code_change_request` tool. Claude reads the repo, makes the change, fixes its
own errors recursively, and commits — all gated by an access policy so Ava can
never silently change anything security-critical.

## When to use

Call `code_change_request` whenever the user:

- Asks to change, add, remove, fix, refactor, or improve anything in the app —
  the phone UI, a bridge module, an MCP tool, a skill, docs, styling, behavior.
- Reports a bug or broken behavior and wants it fixed.

AND whenever **you (Ava) notice a problem in yourself** — an error, a traceback,
a tool that failed, a broken response, something that clearly isn't working. You
are allowed to repair yourself: describe the problem and the fix, and pass the
exact error text in `context`. This is self-recursive fixing.

## The tool

**`code_change_request({ "request": "...", "context": "...", "files": ["..."] })`**

- `request` (required): what to change/fix, where, and why — be specific.
- `context` (optional): the exact error message / traceback, expected behavior,
  or constraints. Always include the real error text when self-fixing.
- `files` (optional): repo-relative files to focus on, e.g.
  `"ava_bridge/web/index.html"`, `"agent/mcp_server_content/web/web_search.mjs"`.

## What happens (the access policy)

Claude's edits are classified per file:

- **Auto-applied + committed** — safe files: the phone UI
  (`ava_bridge/web/*.html`), most bridge modules, MCP tools
  (`agent/mcp_server_*/**`), skills, docs. These land immediately.
- **Sent to the user's approval bucket** — protected control-plane files: auth
  (`ava_bridge/auth.py`), config (`ava_bridge/config.py`), the entrypoint
  (`phone_bridge.py`), egress policies (`agent/policies/**`), deploy scripts
  (`run.sh`, `agent/install.sh`), systemd units. These are STAGED and appear on
  the **Learning page** for the user to review the diff and Approve or Reject. They do
  NOT take effect until they approve.
- **Refused** — secrets and biometrics (`.env`, `data/`, `models/`, git
  internals) are never writable.

## After the tool returns

Tell the user plainly what happened, using the tool's result:

- If files were **applied**, name them and mention it's committed.
- If anything is **awaiting approval**, tell the user it's a protected file and they
  need to review + approve it on the **Learning page** before it takes effect.
- If something was **blocked**, say it touched a protected secret and was refused.

Be honest and specific. Never claim a change is live if it's only pending the user's
approval. When self-fixing, briefly explain what was wrong and what you did.
