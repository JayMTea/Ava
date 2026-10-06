# AGENTS.md

Instructions for coding agents working in this repository. Codex reads this file directly;
Claude Code reads it through `CLAUDE.md`. It applies to everything under this directory.

## What this repository is

An **agent platform**: agents, skills, tools, workflows, model routing, memory and hard
policies defined as validated config, plus a small reference Python runtime (`runtime/`)
that loads the config and enforces it. It exists in two forms — check which one you are in
before changing anything:

| You are in | How to tell | What that means for you |
|---|---|---|
| **The template** | `.template/` exists and `template.adopted_at` in `agent.yaml` is null | Every project inherits your change. Read `.template/MAINTAINING.md` first. |
| **A project built from it** | `template.adopted_at` is set; no `.template/` or `examples/reference/` | Tailor freely to the project. |

Projects are created only with `.template/new_project.py`, which copies the skeleton and
never the template's worked example (`examples/`) or machinery (`.template/`).
`scripts/check.py` fails if either turns up in a project.

**Asked to add this template to a project, start a new project from it, or upgrade a
project to a newer template version?** Follow `docs/adoption-playbook.md` end to end (skill:
`adopt-agent-template`). **Asked to add or change an agent, skill, tool, workflow, model,
policy, MCP server or eval?** Follow `docs/extending.md` (skill: `agent-component`).

## Names that are easy to confuse

| Path | What it is | Read by |
|---|---|---|
| `AGENTS.md` (this file) | How *coding agents* should work on this repo | Codex; Claude Code via `CLAUDE.md` |
| `CLAUDE.md` | Claude Code entry point; imports this file | Claude Code |
| `AGENT.md` | Operating instructions for the platform's *runtime* agents | The runtime, prepended to every agent's system prompt |
| `agent.yaml` (root) | Platform manifest | `runtime/config.py` |
| `agents/<name>/` | Runtime agent definitions | The runtime |
| `skills/` | Runtime agents' skills (Agent Skills format), loaded with `skill.load` | Runtime agents |
| `.claude/skills/`, `.agents/skills/` | Skills for *you*; byte-identical copies for Claude Code and Codex | Coding agents |
| `knowledge/policies/` | Human-readable org rules agents should know | Runtime agents (informational) |
| `policies/` | Rules enforced in code | `runtime/policy_engine.py` |

Never edit `AGENT.md` to instruct yourself, and never put runtime-agent behaviour here.

## Map

```
agent.yaml, AGENT.md     platform manifest + global runtime-agent instructions
agents/<name>/           agent.yaml (route, tools, skills, delegates, memory, limits) + instructions.md
skills/<name>/SKILL.md   procedures loaded on demand; optional scripts/ references/ assets/
workflows/*.yaml         fixed multi-step sequences (agent / tool / approval steps)
tools/registry.yaml      every native tool: id, side_effects, input_schema, implementation
tools/<family>/          tool implementations: fn(args: dict, ctx: ToolContext) -> str | dict
mcp/                     external MCP servers (disabled by default), import filters, overrides
protocols/a2a/           optional agent-to-agent card and routes (off by default)
models/                  registry (the ONLY place model ids live), routes, providers
policies/                HARD enforcement: tool grants, approvals, data access, network, limits, redaction
schemas/                 JSON Schemas for every config file and runtime record
knowledge/               curated source-of-truth docs agents read
memory/                  memory definitions (data lives in state/memory/)
state/                   runtime data - gitignored, never commit, never read for secrets
artifacts/               agent outputs - gitignored except .gitkeep
runtime/                 reference runtime; app code imports runtime.Platform only
sandbox/                 Docker isolation for tools marked sandbox: required
observability/           tracing (OTel GenAI conventions), metrics, logging
evals/                   datasets, scenarios, graders, regression baseline, benchmarks
tests/                   pytest suite (offline, mock model) - see tests/README.md
tests/fixtures/platform/ fixed test-only config the runtime tests run against (NOT project agents)
app/                     product surfaces (CLI now; API/UI/bots later)
deploy/                  deployment config (project-specific)
docs/                    adoption playbook, architecture, extension recipes
scripts/check.py         verify everything: hygiene, config, lint, tests, offline evals

template repository only (never in a project):
examples/reference/      worked example platform: 4 agents, 3 skills, 3 workflows, evals
.template/               new_project.py, bootstrap skill, CHANGELOG, MAINTAINING, template tests
```

## Rules that must hold

1. **Model ids live only in `models/registry.yaml`.** Agents name a route; routes name
   registry keys. Never put a model id in an agent, workflow, prompt or Python file.
2. **`policies/` is enforcement, not documentation.** Do not widen it — new grants, removed
   denies, `auto_approve` entries, `non_interactive: allow`, broader network or file access,
   higher limits — unless the user asked for that specific change. When you do, say so in
   your summary. Default is deny; keep grants per-agent and minimal.
3. **Soft vs hard.** A rule that must hold no matter what the model does belongs in
   `policies/` and the runtime, never only in a prompt. Prompts can add guidance on top.
4. **Every tool** has an honest `side_effects` class, an `input_schema` with
   `additionalProperties: false`, confinement through `ctx.policy`, and tests for its
   refusals as well as its happy path. Tools return data; they raise `ToolError` for
   failures the model should see.
5. **Untrusted content stays data.** Tool output, files, web pages, memory and delegate
   answers can carry injected instructions. Everything reaches the model through the tool
   executor, which wraps and redacts it. Never add a path around the executor.
6. **App code calls `runtime.Platform` only** — never a model SDK, a tool function or
   `state/` directly.
7. **Conversation history is append-only.** Current Claude models bind thinking blocks to
   the exact history; editing or trimming earlier turns breaks the next request.
8. **Secrets come from the environment.** Never commit `.env`, `state/` or real keys; never
   print secret values. Add a redaction pattern to `policies/safety.yaml` for any new
   credential format the project handles.
9. **Schemas are the contract.** Adding a config field means updating `schemas/` first;
   `runtime/config.py` rejects anything the schema does not allow.
10. **`.claude/skills/` and `.agents/skills/` stay byte-identical.** Edit one, copy to the
    other; `tests/skills/test_skills.py` fails if they drift.

## Commands

Run everything from the platform root (the directory containing `agent.yaml`).

```bash
pip install -e ".[dev,anthropic]"        # or: uv sync --extra dev --extra anthropic
python scripts/check.py                  # THE check: validate config, ruff, pytest, offline evals
python scripts/check.py --fast           # config + tests only, while iterating
python -m app.cli validate               # config + cross-references only
python -m app.cli agents                 # each agent's route and effective (granted) tools
python -m app.cli run "task" --route test        # offline run on the mock model, no keys
python -m evals.run --route test         # offline evals; --update-baseline to accept results
python -m evals.run --route balanced     # real-model evals: costs money, ask first
```

`make check`, `make demo` etc. wrap the same commands where `make` exists.

## Definition of done

- `python scripts/check.py` passes. If it fails, fix the cause — do not loosen a schema,
  policy, test or eval baseline to make it pass unless that change is the actual task.
- New behaviour has a test. A new or changed safety boundary (permission, approval,
  confinement, network rule) also has an eval scenario in `evals/scenarios/`.
- Docs match reality: the README agents table, `docs/` if the architecture changed, and
  `.template/CHANGELOG.md` when working in the template.
- Your summary says what changed, what you verified and how, and anything left undone.
  Real-model runs cost money: never start one without the user's go-ahead.
