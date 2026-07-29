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
git clone https://github.com/JayMTea/Ava && cd Ava/deploy && ./install.sh
# it finishes by printing a one-time link; open that to set your admin password
```

One command detects your hardware, downloads a model, and gets you chatting in
your browser. Everything after that happens in the in-app **Setup hub**: wire in
your apps, enroll your voice, set budgets.

That gets you chat, on a local model. GPU workloads and the agent that drives
your other apps are opt-in: `AVA_PROFILE=full ./install.sh`, which grants the
agent a root-equivalent Docker socket and so stays your call, not the
installer's. Voice needs one build flag. The [Quickstart](deploy/README.md)
covers all three.

## Learn more

- [What Ava does](docs/capabilities/index.md): every capability, taken apart
- [Why Ava?](README.md): what it is, what it is not, where it stands
- [Quickstart](deploy/README.md): what each profile includes, Docker and bare metal
- [Connect your apps](docs/CONNECT_YOUR_APPS.md): wire in your apps from the browser
