---
template: home.html
title: "Ava: your private, self-hosted AI operating layer"
description: A self-hosted personal AI operating layer. Chat, voice, GPU workloads, and app automation on your own hardware.
hide:
  - navigation
  - toc
---

<!-- Landing page source. Staged to index.md by sync.py (links are
     repo-relative, rewritten exactly like README links). The hero, feature
     grid, and screenshot live in overrides/home.html; this file is only the
     typeset content below them. House style: no emoji, no em dashes. -->

## Get running in minutes

```bash
cd deploy && docker compose --profile gpu up -d   # or: cpu | cloud | full
# open http://localhost:8096 and set an admin password
```

The in-app **Setup hub** handles the rest: detect hardware, download a model,
provision the agent, wire in apps, enroll your voice. Prefer bare metal?
`ava setup && ava doctor && ava up`, then `ava verify` proves every advertised
capability end-to-end. Full guide: [Quickstart](deploy/README.md).

## Under the hood

[![System diagram](docs/assets/architecture.svg)](docs/assets/architecture.svg)

A FastAPI **bridge** (web app, API, dashboard) fronts a sandboxed **agent
runtime** ([NemoClaw](docs/AGENT_RUNTIME.md) by default, with per-tool egress
policies) and an OpenAI-compatible **inference router**. GPU workloads runs
on **the GPU service**. Everything else is a **connector** you drop in.

## Learn more

- [Why Ava?](README.md): what it is, what it is not, honest comparisons
- [Quickstart](deploy/README.md): Docker profiles and bare-metal install
- [Connector SDK](docs/CONNECTOR_SDK.md): add your own app with a manifest
- [Agent runtime](docs/AGENT_RUNTIME.md): the sandbox, tools, and egress policies
- [Architecture](agent/docs/README.md): how the pieces fit
