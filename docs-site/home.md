---
template: home.html
title: "Ava: a private AI assistant that runs on your own computer"
description: A private AI assistant you host yourself. Chat, voice, and app automation on hardware you own.
hide:
  - navigation
  - toc
---

<!-- Landing page source. Staged to index.md by sync.py (links are
     repo-relative, rewritten exactly like README links). The hero and feature
     grid live in overrides/home.html; this file is only the typeset content
     below them. House style: no emoji, no em dashes.
     Every claim here is code-backed: profiles come from deploy/profiles/*.env
     and deploy/install.sh, the claim token from deploy/README.md.

     "Learn more" comes BEFORE the install section on purpose. A visitor who is
     not ready to paste a shell command needs a route out that is not the back
     button; the old order gave them a `git clone` first. -->

## Learn more

| Page | What it covers |
|---|---|
| [What Ava does](docs/capabilities/index.md) | Every capability, one page each |
| [Why Ava?](docs/WHY_AVA.md) | What it is, what it is not, where it stands |
| [Quickstart](deploy/README.md) | What each profile includes, Docker and bare metal |
| [Connect your apps](docs/CONNECT_YOUR_APPS.md) | Wire in your apps from the browser |

## Get running in minutes

One command detects your hardware, downloads a model, and gets you chatting in
your browser.

```bash
git clone https://github.com/JayMTea/Ava && cd Ava/deploy && ./install.sh
# it finishes by printing a one-time link; open that to set your admin password
```

Open that link whole, `?claim=` and all. Everything after that is browser setup:
connect your apps, enroll your voice, set budgets.

??? note "Why the link carries a `?claim=` token"

    It carries a one-time token, unique to your install, that proves the machine
    is yours before Ava lets anyone set the admin password. Browsing to
    `localhost:8096` without it shows a "not claimed yet" notice even on your own
    machine, because under Docker the container sees the bridge gateway rather
    than localhost. The [Quickstart](deploy/README.md) explains it in full.

??? note "On Windows, run `install.cmd` instead"

    Run these two lines in the Command Prompt window you already have:

    ```
    git clone https://github.com/JayMTea/Ava
    cd Ava\deploy && install.cmd
    ```

    `install.cmd` hands the same installer to the bash that ships with Git for
    Windows, because Command Prompt and PowerShell cannot run a shell script
    themselves. The [Quickstart](deploy/README.md) covers the alternatives.

Chat on a local model is the default. Everything else is a profile:
`AVA_PROFILE=agent ./install.sh`.

| Profile | What you get | Pick this if |
|---|---|---|
| `cpu`, `cuda`, `gpu`, `rocm` | Chat on a local model | Nothing to decide, the installer detects which fits |
| `cloud` | Chat against an API key you supply | You would rather not run a model here |
| `agent` | The above, plus the agent that drives your apps | You want Ava to act, not only answer |

!!! warning "`agent` grants a root-equivalent Docker socket"

    It mounts the host's Docker socket into the agent container. That socket is
    **root-equivalent on the host**: anything holding it can do anything root
    can. It is how the agent spawns its sandbox, the walled-off container it
    runs tools inside. Granting it stays your call, not the installer's, which
    is why the profile is not auto-detected. `gpu` runs everything except the
    tool-using agent without it.

Voice is not a profile: it needs a build flag (`AVA_VOICE_DEPS=1`) and a switch
in Setup.

## Who built this

Ava is built and maintained by **Joshua Thompson** - [GitHub](https://github.com/JayMTea)
· [LinkedIn](https://www.linkedin.com/in/joshua-thompson-b89913105).

Open to collaboration, and the contribution worth most right now is a hardware
report from silicon Ava has not been verified on: see
[CONTRIBUTING.md](CONTRIBUTING.md).

Questions, ideas, or just tell me what you are building:
[open an issue](https://github.com/JayMTea/Ava/issues). Security reports go privately
instead - see [SECURITY.md](SECURITY.md).
