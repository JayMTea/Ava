# Adoption playbook

How to put this agent platform into a project — a brand-new repository or an existing
codebase — and how to upgrade a project to a newer template version. Written for a coding
agent (Claude Code, Codex) to execute step by step; readable by a person doing it by hand.

Template source: **`github.com/JayMTea/agent-platform-template`** (private; reach it with an
authenticated `gh`).

## What a project receives — and what it never does

The template repository holds three things. Only the first one ever reaches a project:

| Part | Where | Reaches a project? |
|---|---|---|
| **Skeleton** — the folder structure, runtime, generic tools, policies, models, docs, tests, one placeholder entry agent | everything at the root | Yes, via `new_project.py` |
| **Worked example** — four agents, three skills, three workflows, their evals | `examples/reference/` | **Never** |
| **Template machinery** — `new_project.py`, bootstrap skill, changelog, template tests | `.template/` | **Never** |

The project's agents, skills and workflows are designed for the project in Phase 4. Use the
worked example to learn patterns; never copy its agents across.

**Always create projects with `.template/new_project.py`.** Never copy the template with
`cp`, `rsync`, `robocopy` or by hand. The script copies only the skeleton, never overwrites
a project file, and names everything. `python scripts/check.py` in the project then fails if
template-only content or an unmerged file is present.

Work through the phases in order. Each phase ends in a state you can verify; do not start
tailoring before the untouched skeleton passes its checks in the new location.

---

## Phase 0 — Identify the situation

| Situation | Signs | Path |
|---|---|---|
| **A. New project** | Empty or near-empty repo, or the user wants a new repo | Phase 2, path A |
| **B. Existing project** | Has its own code, README, CI, dependencies | Phase 2, path B |
| **C. Already adopted** | `agent.yaml` with `template.adopted_at` set | [Upgrading](#upgrading-to-a-newer-template-version), or `docs/extending.md` for new components |
| **D. Inside the template itself** | `.template/` exists and `adopted_at` is null | Don't adopt. Read `.template/MAINTAINING.md` |
| **E. Created with GitHub's "Use this template" button** | `.template/` and `examples/` exist, but the git remote is not `agent-platform-template` | Phase 2, path E |

For path B, survey the codebase before asking anything (a read-only subagent works well):
language and package manager; whether top-level `app/`, `tools/`, `runtime/`, `evals/` or
`agents/` already exist; existing `AGENTS.md` / `CLAUDE.md` / `.claude/` / `.agents/`; CI
system; existing LLM code (SDKs, prompts, model ids) the platform should absorb; data
sources, APIs and docs the agents will need.

## Phase 1 — Decide

Infer what you can from the request and the codebase. Ask the user only what you cannot
infer — **once, batched**, each question with a recommended default. (Claude Code: one
AskUserQuestion call. Codex: one message with numbered questions.) If the user said to
proceed without questions, take the defaults and list them in your final report.

| # | Decision | Default |
|---|---|---|
| 1 | **Name** — lowercase-hyphenated | The repository name |
| 2 | **Purpose** — one sentence: what the agents do, for whom | Derived from the request/README |
| 3 | **Entry agent name** — the agent that receives every task | A role name from the purpose (`support`, `analyst`); else `assistant` |
| 4 | **Other agents** — which roles beyond the entry agent | **None to start.** Add a role only when it needs different tools, a different model or different permissions. Map each need the user described to a role or a skill. |
| 5 | **Model providers** — Anthropic, OpenAI, local OpenAI-compatible server | Anthropic. Add `local` if any data is `restricted` (it must never leave the user's infrastructure). |
| 6 | **Data sensitivity** — the most sensitive data each agent touches | `internal` |
| 7 | **Runtime** — keep the reference runtime, or put the config behind an SDK the project already uses (Claude Agent SDK, OpenAI Agents SDK, LangGraph, Claude Managed Agents) | Reference runtime. See `docs/architecture.md` → *Swapping the runtime*. |
| 8 | **Placement** — repository root, or a subdirectory | Path A: root. Path B: `agent/` (always, if any of `app/`, `tools/`, `runtime/`, `evals/`, `agents/` already exists at the root, or the project is not Python). |
| 9 | **Optional parts to remove** — MCP, A2A, sandbox + `shell.run`, `database.query`, `browser.fetch`, `git.*` | Remove every tool family no agent will use. |

Creating a GitHub repository and pushing are outward-facing: confirm the repo name, owner
and visibility (private by default) before doing either.

## Phase 2 — Create the skeleton

Get the template into a temporary directory **outside** the project:

```bash
# bash / zsh / Git Bash
tmp="$(mktemp -d)" && gh repo clone JayMTea/agent-platform-template "$tmp/apt" -- --depth 1
```

```powershell
# PowerShell
$tmp = Join-Path $env:TEMP "apt-$(Get-Random)"; gh repo clone JayMTea/agent-platform-template "$tmp\apt" -- --depth 1
```

### Path A — new repository

```bash
gh repo create <owner>/<name> --private --clone       # an EMPTY repo, not --template
python "$tmp/apt/.template/new_project.py" <name> --name <name> --description "<purpose>" --entry-agent <entry>
```

### Path B — existing repository

```bash
python "$tmp/apt/.template/new_project.py" <repo>/agent --name <name> --description "<purpose>" --entry-agent <entry>
# root placement instead:  python "$tmp/apt/.template/new_project.py" <repo> ...
```

With subdirectory placement the script also puts the coding-agent skills at the repository
root (so Claude Code and Codex find them from anywhere) and skips `.github/`, which GitHub
only reads at the root.

### Path E — repository made with the "Use this template" button

```bash
python .template/new_project.py --in-place --name <name> --description "<purpose>" --entry-agent <entry>
```

This deletes `examples/` and `.template/` and names the project.

### Merge what the script could not place

The script never overwrites. When a project file already existed and differed, it wrote the
template's version beside it as `<file>.from-template` and listed it. Merge each one, then
delete the `.from-template` file — `python scripts/check.py` fails until all are gone.

| File | Rule |
|---|---|
| `AGENTS.md` | **Root placement:** keep the project's content first, then append the template's sections under `## Agent platform`. **`agent/` placement:** nothing to merge in `agent/`; add a short pointer to the root `AGENTS.md`: *"The agent platform lives in `agent/`; see `agent/AGENTS.md`. Run its commands from that directory."* (the script prints this step). |
| `CLAUDE.md` | Make sure it contains `@AGENTS.md`. Keep the project's Claude-specific notes; append the template's *Claude Code specifics*. Same pointer rule for `agent/` placement. |
| `README.md` | Keep the project's README. Merge the generated section (agents table, quickstart) from `README.md.from-template` under an *Agent platform* heading. |
| `.claude/settings.json` | Merge the `permissions.allow` / `permissions.deny` arrays; keep every project entry. |
| `.gitignore`, `.env.example` | Append the template's entries that are missing. (`agent/` placement: the copy in `agent/` already works — git applies nested ignore files.) |
| `pyproject.toml` | Root placement in a Python project: merge dependencies, optional dependencies, pytest and ruff settings; add `runtime`, `tools`, `app`, `evals` to the package list. |
| CI workflow | Add a job running `python scripts/check.py` (with `working-directory: agent` for subdirectory placement) to the project's existing CI rather than adding a second pipeline. If the project's CI lives in a differently named file, the script will have added `.github/workflows/ci.yml` — move its job into the project's workflow and delete it. |
| `Makefile` | Add the template's targets with an `agent-` prefix. |
| `.editorconfig`, `.gitattributes` | Keep the project's. Delete the `.from-template` file. |
| Anything else | Prefer the project's version; port only what the platform needs from the template's. |

Delete the temporary template clone when done.

## Phase 3 — Install and establish a baseline

From the platform root (the directory with `agent.yaml`):

```bash
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev,anthropic]"                       # or: uv sync --extra dev --extra anthropic
python scripts/check.py
```

`check.py` must pass **before any tailoring** (warnings about placeholder text are expected
here). A failure now is a placement or merge problem — fix it while the only change is the copy.

## Phase 4 — Tailor

Order matters: each step depends on the ones before it. Run `python scripts/check.py --fast`
after each step; the validator names the exact file and field when a cross-reference breaks.
Recipes for every step are in `docs/extending.md`.

1. **Mission.** Rewrite the *Mission* section of `AGENT.md` for this project (remove the
   `(placeholder)` marker). Keep the rest unless the project genuinely needs different
   global behaviour.
2. **Entry agent.** Rewrite `agents/<entry>/agent.yaml` (description, route, tools,
   data classification) and `instructions.md` (role, approach, output — with reasons, not a
   list of capitalised rules). Remove the `(placeholder)` markers.
3. **More agents** — only those decided in Phase 1. For delegation, give the entry agent
   `task.delegate` and `delegates_to`. `examples/reference/` in the template shows a working
   orchestrator-plus-specialists setup if you need the pattern.
4. **Tools.** Remove tool families no agent uses (registry entries, implementation folders,
   their tests). Add project tools (wrap the project's own APIs, services and data access)
   with honest `side_effects` and tests for their refusals.
5. **Policies — least privilege.** One grant per agent listing exactly its tools. Set
   `network.yaml` `allow_domains` to the hosts the project needs (it starts empty). Keep
   writes confined to `artifacts/` unless a tool must write elsewhere. Every `external_write`
   and `destructive` tool stays behind approval. Add redaction patterns for the project's
   credential formats. Update `tests/integration/test_project_guarantees.py` in the same
   change if a guarantee deliberately changes.
6. **Models.** Keep the providers the project has; set each agent's route and
   `data_classification`; use the `local` route for `restricted` work.
7. **Memory.** Give write access to the agent that curates each namespace (all start with
   no writers).
8. **Knowledge.** Seed `knowledge/` with the handful of documents the agents need (from the
   project's docs), each with frontmatter (`title`, `owner`, `last_reviewed`, `sources`).
   Curate; don't bulk-import.
9. **Skills.** Add the project's recurring procedures as skills (one capability each,
   precise `description`).
10. **Workflows.** Add fixed sequences the project really runs.
11. **Optional components.** Enable MCP servers, A2A or the Docker sandbox only if Phase 1
    said so — each with its permissions file filled in.
12. **App surface.** Wire `app/` to how the project is used (API route, CLI, bot, job). It
    talks to `runtime.Platform` only and supplies an `Approver` if humans are in the loop.
13. **Evals.** Replace `evals/datasets/starter.jsonl` with real cases: at least three per
    agent, including one edge case. Keep the safety scenarios and add one per boundary you
    open (for example an SSRF scenario once `browser.fetch` is granted). Then
    `python -m evals.run --route test --update-baseline`.
14. **Docs.** Fill the README agents table; describe the agent design in
    `knowledge/architecture/` so agents and people share the same picture.

## Phase 5 — Verify

```bash
python scripts/check.py                               # must pass, with no placeholder warnings left
python -m app.cli agents                              # each agent: intended route and tools, nothing more
python -m app.cli run "<a representative task>" --route test
```

With the user's go-ahead (real calls cost money) and keys in `.env`:

```bash
python -m app.cli run "<a representative task>"
python -m evals.run --suite smoke --route balanced
```

## Phase 6 — Report

Tell the user, briefly:

- where the platform lives and which agents, tools, providers and optional parts were set up;
- every decision taken by default rather than by them;
- which tool families were removed;
- what was verified, and how (`check.py` output, demo run, real-model run or not);
- what is left for them: API keys, `knowledge/` content only they can write, deployment,
  real-model evals.

---

## Upgrading to a newer template version

1. Read the current version: `template.version` in `agent.yaml`.
2. Clone the template and read what changed:
   ```bash
   gh repo clone JayMTea/agent-platform-template "$tmp/apt"
   git -C "$tmp/apt" log --oneline v<current>..HEAD
   git -C "$tmp/apt" diff v<current> HEAD --stat -- . ':!examples' ':!.template'
   ```
   and the matching entries in `.template/CHANGELOG.md`.
3. Port by kind of file (never anything from `examples/` or `.template/`):
   - **Runtime, schemas, scripts, tests (including `tests/fixtures/`), docs, coding-agent
     skills:** take the template's changes, re-applying any local modifications the project made.
   - **Config the project tailored** (agents, policies, models, memory, workflows, evals):
     apply only structural changes — new required fields, renamed keys. Never overwrite the
     project's choices.
4. Set `template.version` to the new version.
5. `python scripts/check.py` must pass. Report what was ported and what was skipped.

## Do not

- Copy the template by hand, or copy anything from `examples/` or `.template/` into a project.
- Keep tool families "just in case" — each is attack surface, context cost and confusion.
- Grant anything beyond the read-only defaults to `"*"`.
- Put model ids anywhere but `models/registry.yaml`.
- Enable `shell.run` without `sandbox/permissions.yaml` `mode: docker` and approval.
- Loosen a policy, schema, test or eval baseline to make a check pass.
- Leave `(placeholder)` text in `AGENT.md` or the agents.
- Run real-model evals or push to a remote without the user's go-ahead.

## Completion checklist

- [ ] Skeleton created with `.template/new_project.py` (never by hand)
- [ ] Every `*.from-template` file merged and deleted; root pointers added for `agent/` placement
- [ ] Baseline `check.py` passed before tailoring
- [ ] Mission and entry agent rewritten; no `(placeholder)` warnings
- [ ] Only the agents, tools and skills the project uses; grants minimal per agent
- [ ] Network allowlist project-specific; approvals on writes outside the workspace
- [ ] Providers, routes and data classifications set
- [ ] `knowledge/` seeded; README agents table filled
- [ ] Real eval cases per agent; safety scenarios kept; baseline recorded
- [ ] `check.py` passes; offline demo run works
- [ ] Report delivered with defaults, removals and next steps
