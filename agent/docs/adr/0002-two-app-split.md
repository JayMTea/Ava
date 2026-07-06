# 0002. Split Experience (`:8445`) and Admin (`:8444`) front doors

- **Status:** Accepted
- **Date:** 2026-06-28
- **Deciders:** project owner

## Context

Ava needs two very different surfaces: a day-to-day **user experience** (voice,
chat, GPU workloads from a phone) and a powerful **admin/control** surface
(OpenClaw dashboard with agent memory, identity, and configuration). Exposing both
through one door would mean the most sensitive controls share an attack surface with
casual daily use.

## Decision

Run two separate front doors, both exposed only via Tailscale:

- **`:8445` — Experience app** (`phone_bridge.py`, FastAPI on `127.0.0.1:8096`):
  voice/chat/image, password-gated with a signed-session cookie.
- **`:8444` — Admin dashboard** (OpenClaw Control on `127.0.0.1:18789`): agent
  control and memory, reached via its own token.

Each binds loopback and is published independently through `tailscale serve`.

## Consequences

### Positive
- Sensitive agent-control surface is isolated from daily-use traffic.
- Different auth models per surface (app session vs dashboard token).
- Either can be taken down or re-secured without affecting the other.

### Negative / trade-offs
- Two services and two `tailscale serve` mappings to operate.

### Neutral / follow-ups
- Both are captured in the manifest `services:` and the network diagram.

## Alternatives considered

- **Single unified app on one port** — rejected: couples the highest-privilege
  controls to the highest-traffic surface.
