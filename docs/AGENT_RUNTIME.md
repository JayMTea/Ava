# Set up the agent

<ul class="ava-steps" markdown="1">
<li markdown="span">[<b>Step 1</b>Install](../deploy/README.md)</li>
<li markdown="span">[<b>Step 2</b>Pick a model](CHOOSE_A_MODEL.md)</li>
<li class="is-now"><span><b>Step 3</b>Set up the agent</span></li>
<li markdown="span">[<b>Step 4</b>Connect your apps](CONNECT_YOUR_APPS.md)</li>
</ul>

**At the end of this step:** Ava has tools, memory that survives a restart, and a
**sandbox** - an isolated container its tools run inside, which is where the
limits on what Ava can reach are actually enforced.

**Time:** two clicks in the app, then a few minutes while it verifies. One
terminal command first if the runtime is not installed yet.

Ava is an **agent**, not just a chat box. Everything past plain conversation is
provided by an **agent runtime**: the component that owns the sandbox, the tools
and the memory. Ava talks to it through one interface
([`ava_bridge/runtime/`](../ava_bridge/runtime/)), so the runtime is pluggable
and its specifics live in a single adapter.

## The default runtime: NemoClaw (recommended)

[**NemoClaw**](https://github.com/NVIDIA/NemoClaw) (NVIDIA, Apache-2.0) is the
default and recommended runtime. It is a reference stack that runs
[OpenClaw](https://openclaw.ai) inside an [OpenShell](https://github.com/NVIDIA/OpenShell)
sandbox. That gives Ava:

| What you get | What it means |
|---|---|
| **Tools and connectors** | Ava's tools run inside the sandbox and reach the outside world only through the bridge's token-gated `/internal/*` routes. Web search/fetch and every connector call execute host-side, so the sandbox never touches the internet or a connector's API directly. |
| **Isolation and egress policies** | An *egress policy* is a list of the network addresses one group of tools is allowed to reach, and nothing else ([agent/policies/](../agent/policies/)). The sandbox is where that list is enforced, along with the filesystem boundary. It is what makes the connector SDK's auto-generated policies mean something. |
| **Persistent, per-conversation memory** | One session id equals continuous memory. |
| **Skills and self-improvement** | The `ava-*` skills and the self-coding loop. |
| **Live chain-of-thought** | The UI streams the agent's real reasoning and tool steps. |

**It is hardware-portable.** OpenShell creates the sandbox from a container image
via the local Docker daemon (a community `openclaw` image, or your own
Dockerfile), so it runs wherever Docker and the runtime run, not only on a
specific GPU or box.

## Set it up

In the app it is two clicks: open **Setup → Agent**, click
**Provision / re-check**, and watch each step verify.

### Step 1: Open Setup → Agent, then the Runtime section

The **Agent runtime** card is the whole status picture: which runtime is
configured, whether its CLI and sandbox are present on this machine, and how
many tools are deployed. The **Provision / re-check** button sits below it.

### Step 2: Click "Provision / re-check"

Provisioning deploys Ava's tools, skills and deny-by-default egress policies into
the sandbox (`agent/install.sh`). Each step reports what it checked and what it
found, and says plainly which one failed if any did.

That's the whole setup. From a terminal, the same flow is:

```bash
ava agent provision --install     # installs the nemoclaw CLI, then guides you
nemoclaw onboard                  # (interactive) configure inference + create the sandbox
ava agent provision               # deploy Ava's tools/policies/skills into it
ava agent status                  # verify: CLI, sandbox, active runtime, health
```

`ava agent provision` is idempotent. Re-run it any time, and after
`nemoclaw <name> rebuild`.

??? warning "Installing the CLI by hand, and pinning it"

    **Installing the CLI:** NemoClaw is installed by NVIDIA's official installer,
    **not** `npm install -g nemoclaw` (that package is an empty stub). It needs
    **Node ≥ 22.16** and a reachable Docker daemon. `ava agent provision
    --install` runs this for you.

    **Pin it to the same ref the container path uses.** `deploy/agent.Dockerfile`
    pins `NEMOCLAW_INSTALL_REF` deliberately, so that installing from `main` here
    would give bare-metal and Docker installs two different agent runtimes: the
    exact drift that ARG exists to prevent. Read the pin out of the Dockerfile
    rather than hardcoding a version in this doc, which is how it went stale
    before:

    ```bash
    REF="$(sed -n 's/^ARG NEMOCLAW_INSTALL_REF=//p' deploy/agent.Dockerfile)"
    curl -fsSL "https://raw.githubusercontent.com/NVIDIA/NemoClaw/${REF}/install.sh" | bash
    ```

    NemoClaw is pre-1.0 and ships roughly weekly, with rebuild-on-upgrade notes
    on some releases, so treat the pin as load-bearing and bump it deliberately
    (`https://github.com/NVIDIA/NemoClaw/tags`), not by drifting onto `main`.

**Inference endpoint for `nemoclaw onboard`**: point the agent at Ava's router,
`http://host.openshell.internal:8010/v1`. The router starts inside `ava up`
automatically (embedded), fronts whatever backends `ava.yaml` declares, and
gives the agent failover and perf logging for free. If you expose the router
beyond loopback (`inference.router.host: 0.0.0.0`), also configure the bearer
token from `$AVA_HOME/secrets/router_token`.

## The fallback: Direct (tool-less) chat

When no runtime is present **and** `agent.required` is false (the default), Ava
talks to the inference endpoint directly: a working but **tool-less** assistant (no tools, no sandbox, no agent memory; it
replays recent history for continuity). This is the graceful floor so a fresh
install or unsupported box is not a dead end. It is the on-ramp, not the
destination. Force it explicitly with `agent.enabled: false` or
`agent.runtime: direct`, and make the full runtime mandatory instead with
`agent.required: true` ([reference](AGENT_RUNTIME_REFERENCE.md)).

| | NemoClaw (full) | Direct (floor) |
|---|:--:|:--:|
| Tools / connectors | Yes | No |
| Sandboxed + egress-policed | Yes | No |
| Persistent agent memory | Yes | replayed history |
| Live chain-of-thought | Yes | No |
| Works with zero setup | needs provisioning | Yes |

## Full agent in Docker (the `remote` runtime)

By default the Docker image runs the **tool-less** Direct floor: NemoClaw needs
a Docker daemon to spawn its sandbox, which the bridge container does not have.
The `remote` runtime closes that gap without putting Docker-in-Docker in the
bridge. A dedicated **`agent`** container (Node + `nemoclaw` + the host Docker
socket) runs the sandbox and exposes it over HTTP; the bridge talks to it via
`RemoteRuntime`, so tools, memory, and live chain-of-thought all work. The
bridge does not care that the runtime is across the network.

[![The remote runtime: bridge, agent container, sandbox](assets/agent-remote-runtime.svg)](assets/agent-remote-runtime.svg)

!!! warning "This grants root-equivalent access to your machine. Read before enabling."

    The `agent` container mounts `/var/run/docker.sock`. Anything that can talk
    to the Docker socket can start a container that owns the host, so this is
    **root-equivalent on the host**. That is why it is opt-in behind the `agent`
    profile, and why Docker-in-Docker was kept out of the bridge. Run it only on
    a host you trust with that access.

Enable it:

```bash
cd deploy
cp profiles/agent.env .env
docker compose up -d
```

That profile sets the three variables the runtime needs, which only work as a
set. Enabling one alone leaves the bridge in tool-less `direct` mode with
nothing saying why:

```bash
AVA_AGENT_ENABLED=1
AVA_AGENT_RUNTIME=remote
AVA_ROUTER_HOST=0.0.0.0        # so the sandbox can reach the router at ava:8010
```

What the `agent` container does on start, the one host-dependent piece to
validate, and how to add a runtime of your own are in the
[agent runtime reference](AGENT_RUNTIME_REFERENCE.md).

---

**Next step:** [Connect your apps](CONNECT_YOUR_APPS.md), which turns an app you
already run into things you can ask Ava for in plain language.
