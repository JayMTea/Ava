# Why Ava?

**One self-hosted workspace for your assistant and the apps you choose.**
Ava is an independently configurable product. You can clone it, change its
branding and persona, connect your own work, and choose where inference and
agent execution run.

## One connector, several surfaces

An app manifest can declare all of these:

| Surface | What Ava derives |
|---|---|
| App interface | A sidebar entry and a proxied iframe, tool console, or bundled native view. |
| Health | A probe-backed status, including off-by-choice where a feature flag is declared. |
| Performance | An app's declared log, or request timing and status recorded by the proxy. |
| Tools | Static actions, `ava-tools/1` discovery, or an MCP server's tools. |
| Network policy | Generated destinations for compatible sandbox runtimes to enforce. |

Manifests live in your instance's data directory. Adding an iframe or tool-only
connector requires no core source change. Native React views need a frontend
build. Your app keeps its own authentication unless supported SSO is explicitly
configured. Use a separate `apps.origin` for browser isolation; same-origin
embedding trusts the app's JavaScript.

Start with [Connect your apps](CONNECT_YOUR_APPS.md), then use the
[Connector SDK](CONNECTOR_SDK.md) for the full contract.

## What you can use

Chat, memory, uploaded documents, captured charts, app health, and hardware
monitoring share a web interface. Optional voice, web search, remote hardware
monitoring, and domain summaries extend that interface when configured.
The PWA can be installed on a phone.

**Ava ships no default language model.** Connect a compatible local engine or
cloud endpoint. NemoClaw is the default agent adapter, and independently hosted
agents can implement [ava-runtime/1](RUNTIME_SERVICE.md). Direct mode provides
tool-less chat. Streaming, provisioning, control-plane operations, and sandbox
guarantees depend on the runtime you select.

[Using Ava](capabilities/index.md) describes the screens and prerequisites.
Hardware fit, cost, and energy numbers are estimates based on available
measurements; the [evidence tools](EVIDENCE.md) distinguish measured results from
unverified support.

## Independent instances

Keep your configuration, credentials, memory, and integrations under a separate
`AVA_HOME`. Only allow-listed infrastructure connectors ship. There are no
prefilled owner facts, private app dependencies, or maintainer model weights.
Branding and persona belong to your instance. Separate owners use separate
instances, not a shared password.

[Product boundaries](PRODUCT_BOUNDARIES.md) maps each integration point.
[The instance reference](INSTANCE_REFERENCE.md) covers extension installation,
private builds, backup, and restore.

## Data flows follow your configuration

Local storage does not make every configured service local. Cloud inference and
remote agents receive prompts, tools contact their declared services, and model
downloads reach registries. Web search is off by default and requires your own
SearXNG/Tor setup. Voice is off by default and requires voice dependencies.
Read [Security](../SECURITY.md) before selecting trust boundaries for your apps
and runtimes.

[Install Ava](../deploy/README.md), [pick a model](CHOOSE_A_MODEL.md), and
[set up an agent](AGENT_RUNTIME.md) when you need tools.
