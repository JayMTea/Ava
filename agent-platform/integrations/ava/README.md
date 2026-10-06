# Ava app integration

This directory owns Ava's live agent implementation and deployment kit.
The root project installs `ava_agent/` as the `ava_agent` Python package; the
web app imports it for live execution and all agent capabilities. Services
such as authentication and instance configuration remain in `app/backend/`.

The host services discover these
assets directly; `ava agent provision` deploys them through the configured
adapter. Instance data and generated connector tools stay under `AVA_HOME`.
The app's consent and egress enforcement remain in effect.

| Directory | Consumer |
| --- | --- |
| `ava_agent/` | Live turns, adapters, inference, memory, tools, skills, consent, provisioning |
| `mcp/servers/`, `mcp/shared/` | Host tool discovery and sandbox provisioning |
| `skills/` | Host skill registry and sandbox skill installer |
| `policies/egress/` | Host policy inventory and sandbox egress enforcement |
| `sandbox/` | Agent image, install script, and snapshot utility |
| `tools/` | MCP module scaffolding script and templates |

The persona source is `../../agents/ava/persona.md.tmpl`; the host's
`agent-platform/integrations/ava/ava_agent/render_persona.py` resolves instance placeholders before deployment.
Run `ava agent provision` from the host environment, or invoke
`bash agent-platform/integrations/ava/sandbox/install.sh` from the repository root.
The script resolves app paths from its own location and honors `AVA_HOME`.

These assets use the app adapters' formats. The reference runtime does not load
them as native `skills/`, `tools/`, or `policies/` entries. Its native directories
retain the template's contracts and default-deny enforcement.

The host suite validates this package: `python -m pytest tests/unit -q` from
the Ava root. Lint it with `ruff check --config ruff.toml
agent-platform/integrations/ava/ava_agent`. The template's check validates its
native runtime separately. Keep both checks passing when changing shared assets.
