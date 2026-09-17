# Ava

**A self-hosted assistant and workspace for the apps you choose.**

Ava combines chat, connected app interfaces, agent tools, and operational
status views in one web app. Run an independent instance, choose an inference
backend and agent runtime, and add your own integrations through manifests.
No maintainer account, private app, model checkpoint, or hosted Ava service is
required.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Self-hosted](https://img.shields.io/badge/self--hosted-yes-success.svg)](deploy/README.md)

## What ships

| Capability | Behavior and prerequisites |
|---|---|
| Chat | Uses your configured inference backend or agent runtime. **No default language model or weights ship.** |
| Connected apps | A `connector.yaml` can declare a sidebar view, health probe, performance source, tools, credentials by reference, and generated egress policy. |
| Agent | NemoClaw is the default adapter; gateway, remote, direct, external service, and installed Python adapters are supported. Tools, streaming, and sandbox enforcement depend on the adapter. |
| Memory and data | Instance-local chat, recall, uploads, audit records, and app artifacts. Memory controls and artifact exports are in the app; full inventory and backup use the CLI. |
| Operations | Hardware detection, model fit estimates, service health, inference performance, and cost/energy estimates where measurements are available. |
| Optional features | Voice, web search, remote hardware monitoring, and domains require configuration. Voice and web search are off by default. |
| Mobile | Install the web app as a PWA; microphone and service-worker features need HTTPS or localhost. |

See [capabilities](docs/capabilities/index.md) for the current app surfaces and
[product boundaries](docs/PRODUCT_BOUNDARIES.md) for the extension contracts.

## Quickstart

Clone this repository or your fork, then run the Docker installer on Linux/WSL2:

```bash
cd Ava/deploy
./install.sh
```

The installer detects hardware and prints a one-time claim link for setting the
admin password. Choose a model/backend during setup; inference is unavailable
until one is configured and running. The app's setup and management surfaces
remain usable without a model.

For an editable source install on Linux/WSL2:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
export AVA_HOME="$HOME/.ava"
ava setup
ava up
```

Use **Setup** to configure your inference backend, agent, and apps. `ava doctor`
reports missing dependencies and returns nonzero until inference can answer.
`ava verify` checks configuration wiring and generated-resource drift; it is
not proof that every optional service works.

For local inference, choose a model first, download configured models with
`ava models pull --auto`, and start the chosen engine (`ollama serve` or
`bash deploy/local-serve.sh` for NVIDIA/vLLM). `ava up` starts the web app;
it does not start those native engines. Follow the engine-specific install guide.

The installer reports a hardware verification tier. For example:

```text
Platform: Apple Silicon (Mac mini / Studio / laptop) [ci-simulated]
```

This is a support classification, not a measurement of your machine.

For Apple Silicon, use the [native installation guide](deploy/README.md#apple-silicon-mac-mini-studio).
Native Windows Python is not supported; use WSL2 or Docker Desktop.
The [installation guide](deploy/README.md) covers profiles, voice dependencies,
and the separately installed SearXNG/Tor services needed for web access.

## Connect your own work

Install app manifests under `$AVA_HOME/connectors/<id>/connector.yaml`, or use
**Setup → Connectors → Connect an app**. Only the infrastructure connectors in
`connectors/builtins.json` are inherited from the source checkout.

HTTP APIs, `ava-tools/1` discovery, MCP servers, and device connectors have
documented interfaces. An iframe or tool-only connector needs no frontend
rebuild. A native React extension requires a private frontend build.

The proxy authenticates access to Ava and strips Ava's session cookie before
forwarding requests. Your app retains its own authentication unless you
explicitly configure supported SSO. Configure `apps.origin` for browser origin
isolation; the same-origin compatibility mode trusts the embedded app's code.

- [Connect an app](docs/CONNECT_YOUR_APPS.md)
- [Connector SDK](docs/CONNECTOR_SDK.md) and [runnable examples](examples/README.md)
- [External agent service: ava-runtime/1](docs/RUNTIME_SERVICE.md)
- [App-neutral chart and artifact contracts](docs/ANALYTICS_ARTIFACTS.md)
- [Independent instances, extensions, backup, and restore](docs/INSTANCE_REFERENCE.md)

## Your instance, your choices

Set `AVA_HOME` outside the checkout to separate configuration, credentials,
memory, models, and integrations from product source. Independent owners need
independent instances and credentials; Ava is single-owner, not multi-tenant.
Name, appearance, and persona are configurable. No owner facts are prefilled.

Data locality depends on what you connect. Cloud inference and remote agents
receive prompts; configured tools and services receive their requests; model
downloads contact their registries. Web search contacts search providers through
your SearXNG instance. The selected runtime owns sandbox and network enforcement;
trusted Python extensions execute with the bridge user's permissions.

[Security and data flows](SECURITY.md), [memory](docs/MEMORY.md),
[persona](docs/PERSONA.md), and [branding](docs/BRANDING.md) describe these choices.
[Evidence bundles](docs/EVIDENCE.md) report measured checks and their limits.

## Project

Contributions and hardware reports are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md),
[CHANGELOG.md](CHANGELOG.md), and [CITATION.cff](CITATION.cff).
Report security issues privately through [SECURITY.md](SECURITY.md).

Ava is available under [Apache-2.0](LICENSE). Forking, modification, self-hosting,
and commercial use are supported under that license. Keep the required license
and attribution notices; see [NOTICE](NOTICE) and [TRADEMARK.md](TRADEMARK.md).
Models and third-party components have their own terms.
