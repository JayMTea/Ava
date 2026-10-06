# Ava coding instructions

Read [CLAUDE.md](CLAUDE.md) for the app's development conventions and
[docs/REPOSITORY_LAYOUT.md](docs/REPOSITORY_LAYOUT.md) for the source map.

The adopted platform is in `agent-platform/`. Changes inside it also follow
[agent-platform/AGENTS.md](agent-platform/AGENTS.md) and its extension recipes.
Its reference runtime is an independent Python package: use its own virtual
environment and run `python scripts/check.py` from that directory. Do not merge
its native `app` or `runtime` packages into the host app's interpreter.

The app's live agent package is `ava_agent`, installed from
`agent-platform/integrations/ava/ava_agent/` by the root `pyproject.toml`. All
agent execution, inference, tools, skills, memory, consent and provisioning stay
there. App services belong in `app/backend/`; developer utilities in `scripts/`.
Do not recreate root `runtime/`, `tools/`, `state/`, or compatibility re-exports.

The app's deployment kit is `agent-platform/integrations/ava/`; preserve its
adapter contracts and instance paths. Runtime data stays under `AVA_HOME` or
the platform's ignored `state/` and `artifacts/` directories. Never commit it.

Host validation: `ruff check .`,
`ruff check --config ruff.toml agent-platform/integrations/ava/ava_agent`, `python -m pytest tests/unit -q`, and
`bash tests/integration/run.sh --backend` on Linux/WSL. Changes to shared agent
assets must pass both the host suite and the platform check. UI changes also
require the frontend checks documented in `CLAUDE.md`.
