# 0001. Single-source-of-truth architecture pipeline

- **Status:** Accepted
- **Date:** 2026-06-28
- **Deciders:** project owner

## Context

Architecture diagrams and docs rot. As Ava gained services, MCP tools, ports, and
egress policies, hand-maintained diagrams and a hand-written services table drifted
from reality almost immediately. We needed documentation that is **provably** in
sync with the running system, not aspirational.

## Decision

Treat `agent/docs/architecture.yaml` (generated locally per deployment) as the **single source
of truth (SSOT)**. A generator/validator (`agent/docs/arch.py`):

- generates the system, network, policy, and security diagrams (D2) and a services
  table into the deployment-local dev notes (between the `ARCH:services` markers)
  from the manifest, and
- **drift-checks** the manifest against the running system (systemd units, listening
  ports, MCP tool modules, egress policy files).

Automation enforces it on a configured deployment: a systemd path-watcher regenerates
on manifest change, and an optional git pre-commit hook blocks commits when
`arch.py check --strict` finds drift. A fresh clone has no manifest, so every
`arch.py` subcommand is a clean no-op. Ava can read and update the manifest herself
through five `architecture` MCP tools.

## Consequences

### Positive
- Diagrams and docs stay 1:1 with the manifest, and the manifest is drift-checked
  against the running system before every commit on a configured deployment.
- One place to change styling, structure, and facts.
- Ava can introspect and safely self-update her own architecture (drift-gated).

### Negative / trade-offs
- A learning curve: contributors must edit the manifest, never the generated files.
- The generator is custom code that must itself be maintained.

### Neutral / follow-ups
- New diagram types are added as new builder functions registered in `render()` and
  the freshness check (see ADR-0004 for the layout engine).

## Alternatives considered

- **Hand-drawn diagrams (Lucidchart/Confluence/draw.io)** - rejected: rots, no drift
  detection, lives outside version control.
- **Diagrams-as-code without drift checking** - rejected: still drifts from the
  *actual* systemd/ports/tools state.
