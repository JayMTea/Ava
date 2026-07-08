---
template: home.html
title: Ava — your private, self-hosted AI operating layer
description: A self-hosted personal AI operating layer — chat, voice, GPU workloads, app automation, on your own hardware.
hide:
  - navigation
  - toc
---

<!-- Landing page source. Staged to index.md by sync.py (links are
     repo-relative, rewritten exactly like README links). The hero, feature
     grid, and screenshot live in overrides/home.html — this file is only the
     typeset content below them. -->

## Get running in minutes

```bash
# Docker (plug-and-play) — pick a profile for your hardware:
cd deploy && docker compose --profile gpu up -d   # or: cpu | cloud | full
# open http://localhost:8096 — the first screen prompts you to set an admin password
```

From there the in-app **Setup hub** walks you through the rest — detect your
hardware, download a fitting model, provision the agent, wire in your apps,
enroll your voice. Prefer bare metal? `ava setup && ava doctor && ava up`; then
`ava verify` proves every advertised capability end-to-end. Full guide →
[Quickstart](deploy/README.md).

## Under the hood

[![System diagram](docs/assets/architecture.svg)](docs/assets/architecture.svg)

A FastAPI **bridge** (web app + API + dashboard) fronts a pluggable **agent
runtime** (tools, skills, memory — [NemoClaw](docs/AGENT_RUNTIME.md) by default,
sandboxed with per-tool egress policies) and an OpenAI-compatible **inference
router** for the local model. GPU workloads runs on **the GPU service**; everything
else — the apps Ava monitors and drives — is a **connector** you drop in.

## Learn more

- [Why Ava?](README.md) — what it is, what it is *not*, and honest comparisons
- [Quickstart](deploy/README.md) — Docker profiles and bare-metal install
- [Connector SDK](docs/CONNECTOR_SDK.md) — add your own app with a manifest
- [Agent runtime](docs/AGENT_RUNTIME.md) — the sandbox, tools, and egress policies
- [Architecture](agent/docs/README.md) — how the pieces fit together
