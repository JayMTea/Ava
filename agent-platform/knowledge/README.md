# knowledge/ — curated source of truth

What the agents should *know* about this project, written and reviewed by people. Agents
read it with `filesystem.read` (and should check here before the web), so everything in
it must be correct, current and safe to show to any agent.

| Folder | Holds |
|---|---|
| `architecture/` | How the system is built: components, data flow, decisions and their reasons. |
| `documentation/` | How to use and operate it: guides, runbooks, API references. |
| `product/` | What it is for: users, requirements, terminology, roadmap. |
| `policies/` | Human-readable organizational rules (style, compliance, escalation). |

## Rules

- **Curated, not collected.** Add a document because an agent needs it to do its job well,
  not because it exists somewhere. Stale knowledge is worse than none.
- **Frontmatter on every document** so agents can judge freshness:

  ```markdown
  ---
  title: Payment service architecture
  owner: payments-team
  last_reviewed: 2026-10-01
  sources: [https://internal.example.com/adr/0042]
  ---
  ```

- **No secrets or personal data.** Everything here can end up in a model context.
- **`knowledge/policies/` is not `policies/`.** Text here *informs* agents. The YAML in the
  top-level `policies/` *enforces* rules in code. If a rule must hold, it belongs there.
- **Memory vs knowledge.** Memory (`state/memory/`) is what agents learn while working;
  knowledge is what people decide is true. Promote a memory into knowledge by having a person
  write it here.
