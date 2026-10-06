---
title: Ava privacy and execution boundaries
owner: Ava maintainers
last_reviewed: 2026-10-06
sources:
  - "Ava repository: SECURITY.md"
  - policies/data-access.yaml
  - policies/tool-permissions.yaml
  - policies/approvals.yaml
---

# Privacy and execution boundaries

Ava's reference agent classifies tasks as restricted and selects the local route.
Configure LOCAL_LLM_BASE_URL to an owner-controlled OpenAI-compatible service and
LOCAL_LLM_MODEL to its served model through the model registry's environment
substitution. Cloud providers from the template remain available for deliberate
configuration, but are ineligible for Ava's restricted work.

Only filesystem.read, filesystem.list, memory.recall, and skill.load are granted.
Secrets, platform state, and paths outside the platform are not directly readable.
Memory has no writers. Shell execution, external MCP connections, and A2A exposure
remain disabled. External writes and destructive tools require human approval;
noninteractive approval is denied. Read-only questions cannot silently become
connector actions or deployments.

The host app separately enforces its existing connector consent, audit, and
sandbox policies. Its stored conversations and credentials remain in AVA_HOME.
The reference runtime's own task/checkpoint data is under its ignored state/;
outputs are under its ignored artifacts/. Do not commit either runtime's data.
