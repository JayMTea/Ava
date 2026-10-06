---
name: agent-component
description: Add, change or remove a component of an agent platform built from the agent platform template - an agent, runtime skill, tool, workflow, model or route, provider, policy, MCP server, memory namespace, eval case or app surface. Use whenever the work touches agents/, skills/, tools/, workflows/, models/, policies/, mcp/, memory/ or evals/ in a repository that has agent.yaml at its platform root.
---

# Change an agent platform component

Recipes for every component are in `docs/extending.md`; read the relevant section first.
Each recipe lists all files that must change together. The rules that must always hold are
in `AGENTS.md` → *Rules that must hold*.

## Workflow

1. Find the platform root (the directory with `agent.yaml`); run every command from there.
2. Read the recipe, then the existing files you will touch — match their style.
3. Make the change across **all** files the recipe lists. The common misses:
   - a tool listed on an agent but not granted in `policies/tool-permissions.yaml`
   - a removed agent still named in `delegates_to`, a workflow step, an eval or `protocols/a2a/routes.yaml`
   - a skill added to an agent without the `skill.load` tool
   - a model id written somewhere other than `models/registry.yaml`
   - a new config field not added to its schema in `schemas/`
4. Add tests: happy path and refusals for tools; an eval scenario for any new or changed
   safety boundary; dataset cases for a new agent.
5. `python scripts/check.py` — must pass without loosening any policy, schema, test or
   baseline that isn't the subject of the change.
6. Summarize what changed, anything you widened in `policies/` and why, and how you verified it.

## Judgement calls

- Prefer a **skill** over a new agent unless the role needs different tools, model or
  permissions.
- Prefer a **workflow** over delegation when the steps are known in advance.
- Classify a tool's `side_effects` by the worst thing it can do, not the usual thing.
