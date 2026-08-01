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
# Windows: run this in Git Bash, NOT Command Prompt or PowerShell
git clone https://github.com/JayMTea/Ava && cd Ava/deploy && ./install.sh
# it finishes by printing a one-time link; open that to set your admin password
```

Git Bash is a separate application, not a command you type: press the Windows
key, type `Git Bash`, press Enter, and run the command at the `$` prompt. It
ships with Git for Windows, so cloning the repo means you already have it.
Neither Command Prompt nor PowerShell can run a shell script.

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

## Who built this

Ava is built and maintained by **Joshua Thompson** — [GitHub](https://github.com/JayMTea)
· [LinkedIn](https://www.linkedin.com/in/joshua-thompson-b89913105).

Open to collaboration, and there is one contribution worth more than the rest right
now: Ava claims four first-class hardware families and only some are verified on real
silicon — the others are labelled `ci-simulated` because nobody has run them. If you
have an Apple Silicon Mac, an AMD Strix Halo, a discrete Radeon or a plain x86 box,
`python3 tools/ondevice_check.py --record --json` produces exactly the evidence the
support matrix is missing. Either it promotes a row from claimed to verified, or it
hands back a defect list — both are useful, and the second is more useful.

Questions, ideas, or just tell me what you are building:
[open an issue](https://github.com/JayMTea/Ava/issues). Security reports go privately
instead — see [SECURITY.md](SECURITY.md).
