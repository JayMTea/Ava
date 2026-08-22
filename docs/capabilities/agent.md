# The agent: tools, skills & memory

On her own, Ava can talk. **The agent is what lets her act**: search the web,
read a document you uploaded, call one of your connected apps, or turn a light
off. It runs in a **sandbox**, a
locked-down container with no network of its own, so every single thing it does
has to go out through a short, enumerated list of routes on Ava's bridge.
Nothing it can reach is implicit.

[Set up the agent](../AGENT_RUNTIME.md) is the page that turns it on, in two
clicks. **This page is what it can do once it is on, and what it is not allowed
to do.** The short version: everything it can reach is enumerated and
capability-scoped, and nothing it does writes to your source tree.

---

## A pluggable seam with three real implementations

`ava_bridge/runtime/` defines one `AgentRuntime` interface (`available()`,
`run_turn()`, plus optional `exec` / `session_file` / `provision` / `status`)
and a registry keyed by the `agent.runtime` setting. Three implementations ship:

| `agent.runtime` | What runs | Tools | Live CoT |
|---|---|:--:|:--:|
| `nemoclaw` *(default, alias `openclaw`)* | [NemoClaw](https://github.com/NVIDIA/NemoClaw) (NVIDIA, Apache-2.0) running OpenClaw inside an OpenShell sandbox, driven in-process by the bridge | Yes | Yes |
| `remote` | The Docker split: a separate **agent** container owns the `nemoclaw` CLI and the Docker socket, and the bridge drives it over HTTP | Yes | Yes |
| `direct` *(alias `none`)* | The explicit tool-less floor: an OpenAI-compatible call to Ava's inference router with recent history replayed for continuity | No | No |

[![The remote runtime: the bridge container, a separate agent container holding the nemoclaw CLI and Docker socket, and the sandbox it spawns](../assets/agent-remote-runtime.svg)](../assets/agent-remote-runtime.svg)

!!! warning "The `remote` runtime grants root-equivalent access to your machine"

    The `agent` container mounts the host Docker socket
    (`/var/run/docker.sock`). Anything that can talk to that socket can start a
    container that owns the host, so this is **root-equivalent on the host**.
    That is how it spawns the sandbox, and it is why the split is opt-in behind
    the `agent` compose profile rather than on by default, and why
    Docker-in-Docker was kept out of the bridge. Run it only on a host you trust
    with that access. Full instructions:
    [Set up the agent](../AGENT_RUNTIME.md).

Adding a fourth is a file: implement the interface in
`ava_bridge/runtime/<name>.py`, register it, select it with
`agent.runtime: <name>`. Ava's core only ever talks to the interface.

```yaml
agent:
  runtime: nemoclaw       # nemoclaw | openclaw | remote | direct | none
  required: false         # true -> a missing runtime is a loud error
  enabled: true           # false -> force the Direct floor
  sandbox: my-assistant
  agent_id: main
```

`ava agent status` prints the resolution from a terminal; **Setup → Agent**
shows it as status rows.

??? note "How `remote` stays a mirror rather than a reimplementation"

    The agent container runs `ava_bridge/agent_runtime_server.py`, a shim that
    wraps the *same* `NemoClawRuntime`. Every route on that shim except
    `/healthz` is rejected with `401` unless it carries `X-Ava-Agent-Token`,
    compared with `hmac.compare_digest` against the shared secret both
    containers mount. The bridge's side probes `/healthz` and treats the runtime
    as available only when it answers `ready: true`. The container's entrypoint
    onboards the sandbox before that flips, so a half-provisioned agent never
    gets traffic.

??? note "Resolution is honest about degradation, and deliberately strict"

    Two functions do the work, and the difference between them is the whole
    policy:

    - **`configured()`** returns what you asked for: the registry entry for
      `agent.runtime`, defaulting to `nemoclaw`.
    - **`active()`** returns what will actually serve the turn: the configured
      runtime if `available()`, otherwise the Direct floor.

    `gate()` layers `agent.required` on top. When the configured runtime is
    missing and `agent.required: true`, it returns an explicit error string, and
    the turn path raises it rather than quietly serving a tool-less reply that
    looks like a normal answer:

    ```
    The agent runtime (NemoClaw) is required but isn't available. Provision it
    with `ava agent provision --install`, or set `agent.required: false` in
    ava.yaml to allow tool-less direct chat.
    ```

    The availability probe itself is deliberately strict. `NemoClawRuntime`
    reports available only when the agent is enabled in config **and** the CLI
    resolves on disk **and** the sandbox exists, with the result cached about
    30 s, so an install or an `onboard` is picked up within half a minute. An
    *indeterminate* probe (timeout, CLI error) counts as **unavailable** on
    purpose. That reads backwards for a codebase whose usual instinct is
    "degrade, never brick", and the reason is in the source: here the graceful
    degradation *is* Direct. Checking only the CLI once made `active()` pick
    NemoClaw with no sandbox behind it, so every turn burned the full tool
    timeout and returned a canned failure. A working tool-less assistant beats a
    runtime that takes two minutes to fail.

---

## Skills

A **skill** is a folder with a `SKILL.md`: YAML frontmatter (`name` and
`description`, plus optional `title` / `summary` / `category` / `icon` /
`tools` / `app`) and a markdown body that coaches the model on *when* and *how*
to use its tools. The filesystem is the registry. Drop a folder in and it
appears, with no code change and no registration step.

Two roots are scanned, both optional to extend:

1. `agent/skills/<id>/SKILL.md`, the core kit. Five skills ship:
   `ava-architecture`, `ava-devices`, `ava-knowledge`, `ava-weather`,
   `ava-web`.
2. `<overlay>/skills/<id>/SKILL.md`, a private overlay (`AVA_OVERLAY`, default
   `overlay/agent`), so a fork keeps its own skills out of the shared repo.

**Setup → Agent → Skills** renders that catalogue with a per-skill deploy state,
because a skill sitting in the repo is not a capability the agent actually has
yet:

| Badge | Meaning |
|---|---|
| *live* | the `SKILL.md` sha256 matches what was last installed into the sandbox |
| *edited · re-provision* | the file changed since it was installed |
| *not deployed · re-provision* | present in the repo, absent from the deploy manifest |
| *provision to load* | the agent has never been provisioned, so deploy state is genuinely unknown rather than "missing" |

Each card expands to the full `SKILL.md` body (lazy-loaded from
`GET /api/hub/agent/skills/{id}`, so the list endpoint stays light) and carries
tool chips, with the owning app's identity accent when the skill names an `app`,
since dynamically discovered tools have no connector prefix to give them away.

!!! note "Categories are yours, not the product's"

    Create one, rename it inline, drag skills between groups, drag headers to
    reorder. All of it persists to `ava.yaml` under `skills.categories` and
    `skills.category_order`, **never** as an edit to a shipped `SKILL.md`, so
    every fork defines its own taxonomy and upstream imposes none. A category in
    the order list is real even while it holds no skills, so "make a category,
    then fill it" works.

---

## Provisioning, as it actually runs

To **provision** is to install Ava's policies, tools and skills into the
sandbox. **Setup → Agent → Runtime** verifies the CLI, verifies the
sandbox, then runs `agent/install.sh`, each step rendered as a check or cross
row with its reason. The same thing from a terminal is `ava agent provision`. It
is idempotent; re-run it any time, and after `nemoclaw <name> rebuild`.

!!! note "The browser button never installs the CLI"

    `POST /api/hub/agent/provision` passes `auto_install=False` on purpose.
    Running a `curl | bash` installer from a web page is not a thing Ava does,
    so that stays a deliberate terminal step
    (`ava agent provision --install`).

??? note "The seven steps `agent/install.sh` runs, in order"

    1. **Bootstrap guard.** The CLI and the sandbox must already exist, or it
       stops with the exact next command instead of failing deep inside a
       policy-add.
    2. **Apply every egress policy**, from `agent/policies/`,
       `agent/policies/generated/` (what `ava connector policies --write`
       emits), and the overlay's equivalents. Deny-by-default per tool group.
    3. **Discover the guard proxy** from `$HTTPS_PROXY` *inside* the sandbox,
       falling back to OpenShell's default gateway address.
    4. **Mint per-group internal tokens** from `$AVA_HOME/data/.internal_token`
       (created `0600` if absent), one derived token per capability group.
    5. **Auto-discover the MCP servers**: any `mcp_server_<category>/` directory
       with a `_server.mjs`, core plus overlay. Each is tarred, base64'd,
       extracted into `/sandbox/.openclaw/mcp_server_<category>/`, and
       **syntax-checked with `node --check`** before it counts as deployed.
    6. **Rewrite `openclaw.json`**: drop the `ava-*` servers it manages (so a
       removed overlay app does not linger), register the discovered set, each
       launched with `--proxy` and its own `--internal-token`, set
       `agents.defaults.systemPromptOverride` from `render_persona.py` and your
       `ava.yaml` identity block, and force `tools.toolSearch = false` so MCP
       tools are native calls rather than search results. Servers you added
       yourself are preserved.
    7. **Install each skill** with `nemoclaw skill install`, then write the
       deploy manifest `$AVA_HOME/data/skills_deployed.json`, a `name` plus
       `sha256` per skill. That file is what the Skills panel diffs to paint
       *live* versus *edited · re-provision*.

---

## Capability-scoped internal tokens

The sandbox has no ambient network access. Everything Ava's tools do host-side
goes through **25 enumerated `/internal/*` routes** on the bridge, and which of
them a given tool may call is decided by three tables rather than by trust.

**One root secret, six derived tokens.** `$AVA_HOME/data/.internal_token`
(`0600`) is the root. Each MCP server is handed
`HMAC-SHA256(root, "ava-internal:<group>")` at launch, never the root secret
itself. The groups mirror the discovered server directories:

```
admin   content   connectors   productivity   system   wellness
```

**A route-prefix scope table.** `ROUTE_SCOPES` maps each `/internal` prefix to
the capability it needs: `/internal/web` needs `web`, `/internal/policies`
needs `policies`, `/internal/config` needs `config`, and so on.
**Longest prefix wins**, and a path with no entry is **refused for every derived
token** (the root token still passes). Forgetting to classify a new route fails
closed, and a test tells its author which entry to add.

**A grant table** says which group holds which capability
(`ava_bridge/security.py`, `INTERNAL_SCOPE_GROUPS`):

| Group | May reach |
|---|---|
| `admin` | `logs`, `perf`, `config`, `policies`, `model` |
| `content` | `documents`, `model`, `web` |
| `connectors` | `connectors` |
| `productivity` | `learning`, `model` |
| `system` | `architecture`, `model` |
| `wellness` | `model` |

??? note "The threat model: the line that is not in that table"

    `content` is the group whose server ships `web/web_fetch.mjs`, the surface
    where prompt injection actually arrives, and it carries **no control-plane
    capability**: not `config`, not `policies`, not `logs`. A fetched page is
    attacker-controlled text holding a real token, so what that token cannot
    reach is the whole property. Before this table was wired, 24 of 25 handlers
    passed no scope at all — least privilege was written down and not enforced.

    The sharpest line used to be `code_change`: `content` not holding the scope
    that let the agent rewrite Ava's own source. That scope, its route and the
    tool behind it are gone entirely, which is a stronger answer than a table
    entry. See `tests/test_security.py::SelfEditingIsRemovedTests`.

    `content` does not carry `connectors` either, and that omission is measured
    rather than assumed: the only routes the connector tools call are
    `/internal/devices/events` and `/internal/connector/<cid>/*`, so they live
    in their own `connectors` group. Adding a scope on the assumption that a
    server probably needs it is the same mistake as leaving `connectors` on
    `content`.

    The check runs centrally in the auth gate, ahead of routing, so it does not
    depend on each new handler's author remembering to opt in. `_TOKEN_GROUPS`,
    `ROUTE_SCOPES` and the grant table are extension seams: a fork or overlay
    that adds its own MCP server contributes a group and its route entries the
    same way core does.

[![Trust zones from the internet down to the sandbox: an untrusted internet and LAN zone, a TLS and auth-gate perimeter, a loopback-only host holding the bridge, its 0600 secrets and Tor-only web egress, and a Docker sandbox with no ambient egress that reaches the bridge only over enumerated /internal routes with a scoped token](../../agent/docs/diagrams/security.svg)](../../agent/docs/diagrams/security.svg)

The same boundary governs apps: the sandbox never speaks to a connector's API or
an MCP server directly. The bridge does that host-side, on its behalf, with an
audit record per call. See [Apps, devices & MCP](connectors.md) and the
[Connector SDK](../CONNECTOR_SDK.md).

---

## Web access

Ava's sandbox never touches the internet directly. Her `web_search` and
`web_fetch` tools call `/internal/web/*`, and the **host** does the work.

**Search** queries a private, loopback-only [SearXNG](https://searxng.org/)
instance (`AVA_WEB_SEARXNG_URL`, default `http://127.0.0.1:8888`). No third
party, no API key, no query leaving through someone's analytics. That endpoint
is a fixed, trusted local address, so it is deliberately *not* run through the
SSRF guard. Being loopback is the point.

**Fetch** is the dangerous one: the host can reach a connected app on `:8000`,
the inference router on `:8010`, cloud metadata on `169.254.169.254`. So the
reader is wrapped in a guard that refuses anything resolving into non-public
address space, and egress is **fail-closed through Tor by default**
(`AVA_WEB_TOR=1`, SOCKS5h on `127.0.0.1:9050`; SearXNG egresses through it too).
If Tor is unreachable the fetch raises rather than silently retrying over
clearnet. Falling back would leak your real IP, which is the one failure mode
the feature exists to prevent.

??? note "Every check the fetch guard applies"

    - **Scheme allowlist**: `http` and `https` only.
    - **Literal private IPs are refused in every mode.** Loopback, private,
      link-local, reserved, multicast and unspecified ranges all fail.
    - **Internal hostnames are refused by name** without a DNS query at all:
      `localhost`, `metadata.google.internal`, and the `.local` / `.internal` /
      `.lan` / `.home.arpa` suffixes among them.
    - **With Tor off, every resolved A/AAAA record must be globally routable.**
      With Tor on, resolution happens inside Tor (`socks5h`), so no DNS query
      leaks to your ISP and no exit node can reach your LAN.
    - **Re-validated on every redirect hop.** Redirects are followed manually so
      each `Location` goes back through the guard: a `302` cannot bounce the
      fetch into `127.0.0.1`. Capped at `AVA_WEB_FETCH_MAX_REDIRECTS` (4).
    - **Size and time caps.** The response is streamed and abandoned past
      `AVA_WEB_FETCH_MAX_BYTES` (2 MiB), bounded by `AVA_WEB_TIMEOUT` (15 s),
      restricted to text-ish content types, and truncated to
      `AVA_WEB_FETCH_MAX_CHARS` (20 000) before Ava ever sees it.
    - **An optional hostname denylist** (`AVA_WEB_DOMAIN_DENYLIST`) on top of
      all of the above.

    Transport flakiness is retried on a fresh circuit; SSRF refusals never are,
    because they are deterministic.

---

## Memory distillation

A scheduler mines recent chat history for durable facts about you and files them
in long-term memory. It is an in-process daemon thread, not a systemd timer, so
it behaves identically bare metal, in Docker and on a Mac. The first cycle runs
after one full interval, so there is no LLM call at boot.

```yaml
features:
  memory: true                 # master switch (default on)
memory:
  distill_interval_hours: 24   # floored at 1h
```

Each cycle reads chat messages newer than a cursor in `memory.db`, asks the
local router for durable facts about you — stable preferences, ongoing projects,
your setup, people you mention recurringly — and stores at most 8. One-off
tasks and small talk are skipped; below four unseen messages it does not call
the model at all. The cursor advances only once a model has actually looked at
those messages, so nothing is distilled twice and nothing is silently dropped
when the router is down.

**Local-only, by construction.** The prompt quotes your conversations verbatim,
so there is deliberately no cloud fallback: if your router cannot answer, the
cycle does nothing and the cursor stays put. Every run that stores something
writes a `memory_distill` row to the audit ledger, and every fact is visible,
correctable and deletable in **Setup → Agent → Memory**. See [Memory](../MEMORY.md).

!!! note "Ava does not edit her own code"

    She could, until this was removed. `code_change_request` handed an
    engineering task to Claude, which read the repo, wrote files, committed them
    as `Ava <ava@localhost>` and restarted the bridge; an access policy decided
    per file whether that happened automatically or waited for you. The tool,
    the skill, the egress policy, the route, the scope, the three modules behind
    it and the API key that paid for it were all removed together.

    Nothing in Ava writes to the repository now. The architecture watchdog
    reports drift instead of committing a fix for it, and `read_config` reads
    `.env` without being able to write it. Restoring any single layer would be a
    partial re-arming, so `tests/test_security.py::SelfEditingIsRemovedTests`
    pins all of them at once.

---

## Honest notes

**Distillation has no cloud fallback, on purpose.** When Ava's own router
cannot complete a cycle, the cycle produces nothing and retries the same
messages next time. There is no key to fall back to and no switch to turn one
on. The prompt is not abstract — it is a bounded transcript of real chat text —
so "the local model was busy" must never quietly become "your chats went to a
third party".

**"Local router" means whatever you configured.** If you pointed the primary
inference backend at a cloud endpoint, distillation's only attempt already
leaves the box. The guarantee here is that Ava adds no *second* destination of
its own, not that your chosen backend is local.

**The Direct floor is genuinely tool-less.** No sandbox, no skills, no
`/internal/*` callbacks, no live chain of thought. It replays recent history for
continuity and nothing more. The distiller still runs, because it is host-side
and talks to the router rather than to a tool. Set `agent.required: true` if you
would rather fail loudly than run in it by accident.

**Provisioned is not the same as current.** `ava agent status` and the Skills
panel report what is deployed *into the sandbox*, diffed by sha256 against the
repo. A skill you just edited reads *edited · re-provision* until you re-run
provisioning. The UI will not claim a capability the agent does not have.

**Everything above competes for the same memory.** The agent's model, a second
model you keep resident and a voice sidecar all want the GPU. See
[Running two models](../ALLOCATION.md) for how Ava arbitrates that, and
[Data, memory & privacy](data.md) for what the audit ledger records about every
decision on this page.

## Where to go next

- [**Set up the agent**](../AGENT_RUNTIME.md) is how you turn all of this on.
- [**Apps, devices & MCP**](connectors.md) is what the agent reaches through,
  and the permission model in front of it.
- [**Operations**](operations.md) is where proposals are reviewed and approved.
- [**Security**](../../SECURITY.md) is the trust model end to end.
