---
title: Ava application and agent platform
owner: Ava maintainers
last_reviewed: 2026-10-06
sources:
  - "Ava repository: docs/REPOSITORY_LAYOUT.md"
  - "Ava repository: agent-platform/integrations/ava/ava_agent/agent.py"
  - "Ava repository: agent-platform/integrations/ava/ava_agent/adapters/__init__.py"
---

# Ava architecture

Ava is a self-hosted assistant with a FastAPI application and a React frontend.
At the repository root, `app/` contains application entry points and the UI;
`app/backend/` contains application services. The installed `ava_agent` package
under `agent-platform/integrations/ava/ava_agent/` owns agent execution, adapters,
inference, memory, tools, skills, approvals, and provisioning. Instance data and credentials resolve beneath the configured `AVA_HOME`.

`agent-platform/` is generated from the official agent-platform-template. Its
`runtime.Platform` loads agents, policies, skills, tools, model routes, memory,
and workflows using the supplied schemas. This reference runtime runs in its own
Python process and virtual environment because its package names also include
`app`.

`integrations/ava/` contains the live `ava_agent` Python package and deployment kit: runtime-specific
skills, five MCP servers, their shared helper, egress presets, and sandbox
provisioning. The app's selected adapter continues to own live conversations and
connector actions. Merely storing that kit here does not grant its tools to the
reference runtime. `agents/ava/persona.md.tmpl` is rendered by the host app for
that deployed identity; `agents/ava/instructions.md` guides the reference runtime.

Use `ava platform validate`, `ava platform agents`, and `ava platform check`
from the repository root. `ava platform run "task" --route test` is an offline
mock run; it verifies wiring, not real-model answer quality.
