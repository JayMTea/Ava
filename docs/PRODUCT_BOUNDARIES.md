# Product and integration boundaries

Ava is a standalone, single-owner web application. Operator identity, private
apps, credentials, model weights, and runtime data belong to an instance.

## Supported seams

| Concern | Configuration or interface | Boundary |
|---|---|---|
| Instance state | `AVA_HOME`, `.env`, `ava.yaml` | An explicit home does not inherit the checkout's environment. Keep homes outside public source. |
| Apps and services | `connectors/<id>/connector.yaml` | Only IDs in `connectors/builtins.json` ship as built-ins. Instance manifests override by ID. |
| Credentials | Manifest environment/file references | Keep values in instance secrets; do not embed them in source, browser bundles, or prompts. |
| App UI | `ui.embed`, `/apps/<id>/`, `apps.origin` | Iframes and tool consoles need no rebuild. Same-origin mode trusts app code; a separate apps origin isolates it from the shell. Apps retain their own login unless SSO is configured. |
| Inference | `inference.backends`, model settings | Bring a compatible engine or API. No language-model default or weights are supplied. |
| Agent execution | `agent.runtime`, `ava-runtime/1`, `runtime_adapters/` | Capabilities and sandbox guarantees come from the selected adapter/service. |
| Backend extensions | `extensions/<id>/extension.yaml`, `extensions.enabled` | Explicitly enabled, trusted Python code with the bridge user's permissions. |
| Frontend extensions | Private build overlay | Compiled code; publish a clean product build separately from an instance build. |
| Persona and brand | Instance configuration and `branding/` | Optional owner facts and presentation, with no core code fork required. |
| Charts and query results | `ava-artifact/3`, `analysis-result/2` | App-neutral schemas; live paths must be declared by the connector. |
| Model allocation | Configured model specs and allocation drivers | Explicit resources and launch commands; no dependency on another app's private coordinator. |

The [instance reference](INSTANCE_REFERENCE.md), [Connector SDK](CONNECTOR_SDK.md),
[runtime service protocol](RUNTIME_SERVICE.md), [artifact reference](ANALYTICS_ARTIFACTS.md),
and [allocation reference](ALLOCATION.md) contain the implementation details.

## A reusable installation

1. Clone Ava or a fork and use the [installation guide](../deploy/README.md).
2. Give each independent owner a separate home, credentials, ports, and runtime
   session namespace. A shared password does not provide tenant isolation.
3. Configure inference and optional capabilities. Setup works before inference
   is available; `ava doctor` reports that incomplete state.
4. Install your connectors and extensions in your instance directory. Keep
   credentials and custom source outside the public checkout.
5. Rebrand through configuration and back up using the instance commands.

## Public source and release checks

The privacy guards cover tracked text, filenames, generated connector material,
runtime directories, and Docker build exclusions. Local private identifiers can
be supplied through `.git/info/private-names`, one regular expression per line.
The file stays local and is resolved through Git's common directory for worktrees.
CI can receive the same rules through the optional `AVA_PRIVATE_NAMES` repository
secret; findings identify files and categories without printing the matching text.
Without it, only the generic checks run; that does not prove that unknown private
names are absent.

Run the checks described in [Contributing](../CONTRIBUTING.md) before publishing.
Review screenshots, recordings, and other binary assets separately: text scanning
cannot prove that they contain no personal data. Use synthetic examples and
remove captures that no longer match the interface.

GitHub Pages stages an explicit list of documents and assets. Missing source
files fail staging, and strict builds check internal links. Repository and Pages
URLs are deployment metadata, configurable for a fork.

These checks protect the next source tree and build. They do not erase earlier
Git commits, releases, cached Pages deployments, forks, or Git author metadata.
History removal and credential revocation require a separate review when earlier
publication is found. Keep detailed privacy findings outside the public tree.
