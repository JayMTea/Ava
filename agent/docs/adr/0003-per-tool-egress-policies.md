# 0003. Per-tool narrow egress policies (least privilege)

- **Status:** Accepted
- **Date:** 2026-06-28
- **Deciders:** project owner

## Context

Ava's agent runs inside an OpenClaw Docker sandbox and can call MCP tools that reach
the network (weather APIs, the host's the GPU service, the host bridge, a connected app). If
the sandbox had broad outbound access, a single compromised or prompt-injected tool
could exfiltrate data or pivot to other services. LLM tool-use is an untrusted-input
boundary by nature.

## Decision

Give **every tool its own narrow egress policy**. Each policy
([`agent/policies/<name>.yaml`](../../policies/)) allow-lists only the exact
host:port + HTTP method/path that one capability needs; everything else is **denied
by default**. Host-callbacks additionally require a scoped
`X-Ava-Internal-Token` bearer and are limited to enumerated `/internal/...`
routes. The manifest declares the
tool↔policy↔egress mapping and drift-checks it 1:1 against the policy files; the
generated policy-trace diagram (rendered locally by `arch.py sync`) visualizes it.

## Consequences

### Positive
- **Blast-radius containment:** a compromised tool can still only reach its single
  declared destination (e.g. weather → Open-Meteo GET only).
- No ambient authority — permissions never leak between tools.
- Auditable: each policy is an explicit, readable contract.

### Negative / trade-offs
- Adding a tool that needs new network access requires writing a new policy and
  re-running `agent/install.sh` after a sandbox rebuild.

### Neutral / follow-ups
- The SSRF guard requires private host-gateway IPs to be explicitly allow-listed.

## Alternatives considered

- **One broad sandbox egress rule** — rejected: defeats least privilege; one bad
  tool compromises everything.
- **No sandboxing (agent on host)** — rejected: removes the confinement boundary
  entirely.
