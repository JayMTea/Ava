@AGENTS.md

## Claude Code specifics

- **Project skills** in `.claude/skills/`: `/adopt-agent-template` (apply this template to a
  project, or upgrade one) and `/agent-component` (add or change an agent, skill, tool,
  workflow, model route, policy, MCP server or eval). Codex has identical copies in
  `.agents/skills/`; keep them in sync.
- **Plan first for adoption.** Adopting the template touches dozens of files and involves
  decisions only the user can make (which agents, which providers, where it lives). Use plan
  mode, batch the open questions into one AskUserQuestion call with recommended defaults,
  then execute.
- **Subagents** are useful for read-only sweeps of an existing codebase during adoption
  (what tools, data sources and docs the project already has). Keep edits in the main thread
  so cross-file consistency stays in one context.
- **Settings:** `.claude/settings.json` pre-approves the check commands and blocks reading
  `.env`. Personal overrides go in `.claude/settings.local.json` (gitignored).
- **Nested placement:** when the platform lives in a subdirectory (e.g. `agent/`), its
  `CLAUDE.md` loads when you work on files there; run commands from that directory.
