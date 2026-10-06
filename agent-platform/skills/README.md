# skills/

Procedures the platform's agents load on demand (the Agent Skills format, agentskills.io).
Empty in the skeleton — add the project's own. Recipe: `docs/extending.md` → *Add a skill*.

```
skills/<name>/
  SKILL.md          frontmatter (name, description) + the procedure
  references/       long reference material, read on demand
  assets/           templates the agent fills in
  scripts/          helper code (needs a sandboxed execution tool to run)
```

- `name` equals the directory name: lowercase, hyphenated, at most 64 characters.
- `description` (at most 1024 characters) says what the skill does **and when to use it** —
  it is all the model sees until it calls `skill.load`.
- An agent gets a skill by listing it under `skills:` and having the `skill.load` tool.

Not to be confused with `.claude/skills/` and `.agents/skills/`, which are skills for the
coding agents (Claude Code, Codex) that work on this repository.
