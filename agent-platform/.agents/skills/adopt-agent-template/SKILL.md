---
name: adopt-agent-template
description: Apply the agent platform template (agents, skills, tools, workflows, policies, models, memory, evals plus a Python reference runtime) to a new or existing repository, or upgrade a project built from it to a newer template version. Use when asked to implement, add, set up, scaffold, adopt or upgrade the agent template or agent platform. Not for adding one agent, tool or skill to a project that already has the platform - use agent-component for that.
---

# Adopt the agent platform template

The procedure lives in `docs/adoption-playbook.md`. Read it in full before acting — this
skill only orients you; the playbook is the source of truth. If the current repository does
not have the playbook yet, it is in the template repository
`github.com/JayMTea/agent-platform-template` (private; shallow-clone it with `gh` to a temp
directory outside the project and read it there).

## The one rule

Only the template's **skeleton** may enter a project, and only through
`.template/new_project.py`. Never copy the template by hand, and never copy anything from
its `examples/` (a worked example) or `.template/` (its machinery). The project's agents,
skills and workflows are designed for the project — the example's are for learning patterns.

## Shape of the work

0. **Identify** the situation: new repo, existing repo, already adopted (upgrade), the
   template itself (don't adopt), or a "Use this template" copy (`--in-place`). Survey an
   existing codebase before asking anything.
1. **Decide** — ask the user only what you cannot infer, once, batched, with recommended
   defaults: name, purpose, entry agent name, other agents (default: none), providers, data
   sensitivity, runtime, placement (root vs `agent/`), tool families to remove.
2. **Create the skeleton** with `new_project.py`. It never overwrites; merge every
   `*.from-template` file it reports using the playbook's rules, then delete it.
3. **Install and baseline**: `python scripts/check.py` must pass *before* tailoring.
4. **Tailor** in the playbook's order: mission, entry agent, more agents, tools, policies
   (least privilege), models, memory, knowledge, skills, workflows, optional components, app
   surface, evals, docs. Run `python scripts/check.py --fast` after each step.
5. **Verify**: `python scripts/check.py` (no placeholder warnings left),
   `python -m app.cli agents`, an offline demo run. Real-model runs only with the user's go-ahead.
6. **Report**: placement, components, defaults taken, removals, verification, next steps.

## Non-negotiables

- Confirm before creating a GitHub repository or pushing (name, owner, private).
- Never widen `policies/` beyond what the project needs; never loosen a check to pass it.
- Model ids only in `models/registry.yaml`.
