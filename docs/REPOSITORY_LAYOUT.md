# Repository layout

The app owns presentation and instance services. The agent platform owns agent
execution and capabilities. There is no root `runtime/`, `tools/`, `state/`, or
artifact placeholder tree.

```text
Ava/
├── AGENTS.md, CLAUDE.md
├── app/
│   ├── cli.py, server.py, router.py, voice.py
│   ├── paths.py                # source locations
│   ├── backend/                # application services and HTTP APIs
│   └── frontend/               # React app, tests, and built bundle
├── agent-platform/             # agent-platform-template v1.1.0
│   ├── AGENT.md, agent.yaml
│   ├── agents/ava/, skills/, workflows/
│   ├── runtime/, app/, tools/   # template runtime and native CLI/tools
│   ├── models/, policies/, schemas/, mcp/, protocols/
│   ├── knowledge/, memory/, observability/
│   ├── sandbox/, deploy/, evals/, tests/, docs/, scripts/
│   ├── state/, artifacts/      # ignored template output
│   └── integrations/ava/
│       ├── ava_agent/          # installed live agent integration
│       │   ├── agent.py, turns.py
│       │   ├── adapters/       # configured execution backends
│       │   ├── alloc/          # inference resource allocation
│       │   └── ...             # tools, skills, memory, consent, provisioning
│       ├── mcp/servers/, mcp/shared/
│       ├── skills/, policies/egress/
│       └── sandbox/, tools/    # deployment and MCP scaffolding
├── scripts/                    # developer diagnostics and maintenance
├── config/, bin/, deploy/      # app configuration, launchers, deployment
├── connectors/, sdk/, examples/
├── tests/unit/, tests/integration/
└── docs/architecture/, docs/site/
```

## Execution and package ownership

The web app imports `ava_agent.agent` and `ava_agent.turns`, which use
`ava_agent.adapters` to execute through the configured backend. Model routing,
tool discovery, memory, approvals, and provisioning are implemented in that same
platform-owned package. The app's `backend/` has no duplicate implementations
or forwarding modules for those capabilities.

The root `pyproject.toml` maps the package name `ava_agent` to its source under
`agent-platform/integrations/ava/`. Install Ava with `pip install -e .`; Docker
images and CI do the same. This is a normal package installation, not a path
injection or import alias. The selected adapter and its grants remain unchanged.

The template's `runtime.Platform` remains the native workflow/tool engine.
`ava platform` invokes it in its own environment. The projects still both define
an `app` package and have different dependency sets, so the native platform CLI
uses an isolated interpreter. Live adapters and the native reference runtime
have distinct contracts; moving code does not silently change engines, expose
MCP tools, or replace stored conversations.

## Validation

```bash
pip install -r config/dependencies/dev.txt -e .
ruff check .
ruff check --config ruff.toml agent-platform/integrations/ava/ava_agent
python -m pytest tests/unit -q
bash tests/integration/run.sh --backend
uv venv agent-platform/.venv
uv pip install --python agent-platform/.venv -e 'agent-platform[dev,openai]'
ava platform check
```

The host suite runs on Linux/WSL. The template suite runs on Windows or Linux.
Host CI checks the live integration with the app; platform CI independently
validates the native runtime, schemas, policies, and offline evals.

## Migration and data

| Retired location | Current owner |
| --- | --- |
| root `runtime/` application services | `app/backend/` (`app.backend` imports) |
| root `runtime/` agent modules, adapters, allocation | `agent-platform/integrations/ava/ava_agent/` (`ava_agent` imports) |
| root `tools/` diagnostic scripts | `scripts/` |
| root empty `state/` and artifact placeholder | removed; native output belongs inside `agent-platform/` |
| old `ava_bridge/`, root Python entry scripts | application packages and the `ava` CLI |
| old `frontend/`, `docs-site/`, `qa/` | `app/frontend/`, `docs/site/`, `tests/integration/` |

Reinstall editable environments after moving source. External Python adapters
must import `AgentRuntime` from `ava_agent.adapters.base`. External scripts should
use `ava` commands or the documented module names.

Instance configuration, databases, model weights, recordings, credentials, and
generated connector material keep their existing `AVA_HOME` locations. They
are not source duplicates. Ignored legacy data paths remain ignored; no user
data is moved or deleted by this source refactor.
