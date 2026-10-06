# agents/

One directory per agent: `agent.yaml` (validated by `schemas/agent.schema.json`) and
`instructions.md`. The directory name is the agent's name.

The skeleton ships a single starter entry agent so the platform runs from the start. Replace
it with the project's own agents — recipe: `docs/extending.md` → *Add an agent*. Add an
agent only when a role needs different tools, a different model or different permissions;
otherwise add a skill.

For a worked multi-agent example (orchestrator delegating to specialists), see
`examples/reference/` in the template repository.
