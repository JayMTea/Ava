# Enforcement

`egress/` contains sandbox network presets applied by `agent-platform/integrations/ava/sandbox/install.sh`.
Permission checks, connector consent, and approvals are implemented in
`app/backend/auth.py`, `app/backend/internal.py`, `agent-platform/integrations/ava/ava_agent/grants.py`, and `agent-platform/integrations/ava/ava_agent/approvals.py`.
`agent.yaml` indexes those implementations; instance settings live in `ava.yaml`.
