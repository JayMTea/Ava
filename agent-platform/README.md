# Ava agent platform

Generated from **JayMTea/agent-platform-template v1.1.0**, commit
`dc106be2f68a2bf4a327261139326449734149fc`, using `.template/new_project.py`.
The upstream reference runtime, tool implementations, schemas, and tests are
preserved. Ava's mission, agent configuration, knowledge, skill, workflow, and
evals tailor the generated project. See [provenance](docs/template-provenance.json).

Coding agents start with [AGENTS.md](AGENTS.md). Runtime agents follow [AGENT.md](AGENT.md).

## Setup and checks

Both the host app and this project define an `app` Python package and have distinct dependency sets.
Use separate virtual environments. From the Ava repository root:

```bash
uv venv agent-platform/.venv
uv pip install --python agent-platform/.venv -e 'agent-platform[dev,openai]'
# With the host Ava CLI installed:
ava platform validate
ava platform agents
ava platform check
ava platform run "Describe your role" --route test
ava platform workflow ava-overview
ava platform evals --route test
```

Alternatively, activate this directory's environment, change into this
directory, and run `python scripts/check.py` or `python -m app.cli ...` directly.
`check` validates config, lints, runs tests, and runs offline evals. The `test`
route returns scripted mock output; it does not perform real inference.

For actual inference, copy `.env.example` to `.env` here and set
`LOCAL_LLM_BASE_URL` and `LOCAL_LLM_MODEL` for your OpenAI-compatible local
endpoint. The `openai` extra supplies that protocol client. No model weights or
credentials ship. The Ava entry agent uses the **local** route with **restricted**
data classification, which excludes remote providers. MCP, A2A, and shell
execution remain disabled. State and artifacts are ignored by Git and Docker.

## Agent and components

| Component | Purpose |
| --- | --- |
| `agents/ava/agent.yaml` | Entry agent for architecture and operating guidance; local route, restricted data |
| `agents/ava/instructions.md` | Instructions for the reference runtime |
| `agents/ava/persona.md.tmpl` | Existing app persona, rendered by the host's `agent-platform/integrations/ava/ava_agent/render_persona.py` |
| `skills/ava-platform-guide/` | Procedure for answering from curated sources |
| `workflows/ava-overview.yaml` | Deterministic reads of architecture and privacy guidance |
| `knowledge/` | Curated, owner-neutral source material |
| `integrations/ava/` | Live `ava_agent` package, MCP servers, skills, egress presets, sandbox image and provisioning |

Ava has four granted tools: `filesystem.read`, `filesystem.list`, `skill.load`,
and `memory.recall`. The template retains its native tool families for extension
using [the component recipes](docs/extending.md); registration alone does not
grant permission. No memory writer is granted.

The host app imports the platform-owned `ava_agent` package for live agent
execution, routing, memory, tools, and provisioning, using the adapter selected
in `ava.yaml`. The root project installs that integration package explicitly. This reference
runtime is callable with `ava platform`; it does not replace live chat or bypass
the app's consent and connector contracts. The deployment kit's formats are kept
in [integrations/ava/](integrations/ava/README.md), outside the template's native
registries. Enabling those tools in the reference agent requires an explicit
adapter and corresponding policy changes.

See [architecture](docs/architecture.md) for the full structure and enforcement
flow, and [the adoption playbook](docs/adoption-playbook.md) for future upgrades.
