# Independent Ava instances

Ava is a single-owner application that other owners can clone, configure and
extend. Each instance has its own `AVA_HOME`, login, integrations, state and
runtime connection. Separate instances are the supported way to serve separate
owners; a shared password is not multi-tenant authorization.

## Start with your own configuration

Use the Docker installer in [INSTALL_REFERENCE.md](INSTALL_REFERENCE.md), or an
editable source installation on Linux/WSL2. Native Windows Python is not supported
by the file-locking components; use WSL2 or Docker Desktop.

```sh
export AVA_HOME="$HOME/.ava-work"
python -m pip install -e .
ava setup
ava doctor
ava up
```

The shell launchers also read the selected home's `.env`, with exported values
taking precedence. The NVIDIA launcher defaults to a product-owned container
name, `ava-inference`. Set `AVA_SERVE_CONTAINER` explicitly when reusing an
existing container or sharing its identity with a separate allocator.

For a second instance, choose another home, bridge port, router port and any
published app-origin port. Select its own inference endpoint or runtime credentials.
`ava setup` preserves existing configuration unless a command says otherwise.

With `AVA_HOME` supplied in the process environment, configuration resolves from
exported environment variables, that home's `.env`, its `ava.yaml`, then product
defaults. The checkout's `.env` is not loaded. `AVA_LOAD_REPO_ENV=1` is an explicit
compatibility option for installations that intentionally share that environment.
Without an explicit home, a source checkout continues to act as its own instance.

Only the connectors listed in `connectors/builtins.json` are inherited from shared
source. Install your apps under `$AVA_HOME/connectors/<id>/connector.yaml`; an
instance manifest overrides a shipped connector with the same ID. See the
[Connector SDK](CONNECTOR_SDK.md) for HTTP, MCP and stdio integrations, consent,
credential references, app origins and generated agent tools.

Brand name, logo, wordmark and colors belong in instance configuration and
`branding/`. The shipped favicon and installed PWA icon remain the Ava mark.
Owner name, location and persona are optional; a fresh instance has no owner facts
or private apps. Locality and tool-access claims depend on the configured runtime.

## Bring your own agent and sandbox

Existing `nemoclaw`, `openclaw`, `openclaw_gw`, `remote`, `direct` and `none`
selections retain their meaning. Use `service` for an independently hosted agent
implementing [ava-runtime/1](RUNTIME_SERVICE.md). Install another Python adapter
without editing the registry:

```text
$AVA_HOME/runtime_adapters/my-runtime/
  extension.yaml
  runtime.py
```

```yaml
# extension.yaml
api_version: ava-extension/1
runtime: runtime.py:Runtime
```

`Runtime` subclasses `ava_bridge.runtime.base.AgentRuntime`, declares
`name = "my-runtime"`, and implements `available()` and `run_turn()`. Then select
`agent.runtime: my-runtime` in `ava.yaml` and restart Ava. Optional methods describe
capabilities, sessions, model information, provisioning and sandbox execution.
Only NemoClaw-compatible adapters opt into `provisioning_layout = "nemoclaw"`.
Other adapters provide their own `desired_state()` and `observe()` contracts.
Never report a capability or successful provision that the adapter cannot perform.

Adapters and backend extensions are trusted Python code executing with the bridge
user's permissions. They are not a sandbox. Prefer a separately hosted runtime for
an independent execution boundary. Failed extension loads appear in system status;
they do not prevent the owner from signing in. Restart after repairing installed code.

## Backend and frontend extensions

For a backend extension, create
`$AVA_HOME/extensions/my-extension/extension.yaml` with
`api_version: ava-extension/1` and `backend: backend.py:register`. Implement
`register(app)` and include `my-extension` in `extensions.enabled`. Entry files and
their relative imports must stay within the extension directory. Routes use the
bridge's existing authentication middleware; choose a distinct route namespace.

Private agent material lives in `$AVA_HOME/overlay/agent` or the explicit
`extensions.agent_dir` / `AVA_OVERLAY` path. The source-checkout route hook
`overlay.ava_bridge.personal_routes` remains supported in its original checkout;
a separate home must intentionally enable `extensions.legacy_routes` to use it.

Frontend additions are compiled into a private image. Public release builds use
tracked source only. To build your own extension image from a reviewable context:

```sh
python deploy/scripts/build_context.py --output /tmp/ava-custom-context \
  --frontend-extensions /path/to/your/frontend/overlay
docker build -f /tmp/ava-custom-context/deploy/Dockerfile \
  -t my-ava:custom /tmp/ava-custom-context
```

The frontend folder follows the existing `frontend/src/overlay` module contract.
Optional `--backend-extensions` and `--agent-extensions` copy deliberately selected
source to the legacy overlay layout. Enable the legacy route hook and mount/configure
agent material when using that compatibility layout with a separate container home.
The context records whether private code was included. Environment files, secret
directories and database files are excluded; do not embed credentials in source.
Backend extensions under `AVA_HOME` need no image rebuild.

## Inventory, backup and restore

```sh
ava instance inspect
ava instance backup --output /path/outside/instance/ava-backup.zip
ava instance restore --source /path/to/ava-backup.zip --target /path/to/new-home
```

Inventory shows configured roots and connector identities, without credentials.
Backups include configuration, secrets, conversations, charts, uploads, voice
enrollment, generated agent material and installed extensions. Downloadable model
weights are omitted. SQLite databases are copied through SQLite's backup API and
checked for integrity. Stop writers before backup if you require one coordinated
point in time across multiple stores; individual SQLite snapshots are consistent.
State owned by an external agent or connected app must be backed up separately.
Source customizations and private frontend build inputs outside the instance home
also need their own source-control or backup policy.

Archives are **confidential and unencrypted**. Files are created owner-readable;
store them on protected storage. Symlinks and temporary database sidecars are not
included. Restore verifies every member's path, size and checksum before publishing
a new directory and refuses to merge with or overwrite an existing instance.
It resets local path overrides and rotates the new home's login signing key.
Your password and application data are retained, but copied browser sessions do
not sign in to the new instance. Review external endpoints, environment-pinned
signing keys, application credentials, connector grants and extension code before
starting a restored home. Checksums detect corruption, not a malicious archive.

Explicit legacy copies leave the source intact and refuse an existing target:

```sh
ava instance adopt-voiceprint --source /path/to/previous/models/voiceprint.npy
ava instance adopt-artifacts --source /path/to/previous/data/analytics-artifacts.db
```

The first accepts ECAPA's 192-dimensional float enrollment. The second validates
and snapshots the chart database into the configured `paths.data`. An explicit
`AVA_LEGACY_VOICEPRINT=1` enables the older shared-enrollment behavior; deleting
enrollment in that mode also deletes its explicitly shared legacy copy. Prefer a
one-time copy for independent instances.

Recorded charts appear in the Data inventory with an audited delete operation.
`data.artifact_retention_days: 0` preserves them indefinitely; a positive value
prunes older charts when the store is accessed. Backups have their own retention
and are not erased by deleting live data. New passwords use salted verifiers;
existing passwords continue working and are upgraded when changed through Setup.

## Deployment and upgrades

Keep deployment overrides and private export mappings outside public source.
The optional [deployment workflow](../deploy/nas/README.md) requires an explicit
stack path and successful CI for the selected commit. Its sync preserves the
existing receiver's instance directories and `deploy/nas` configuration.

Before upgrading an existing instance, back it up and record the running image
digest. Validate the new image using a restored home and distinct ports, then
switch the deployment to the tested image. Keep the previous image and backup for
rollback. A newer database schema must not be opened by an older bridge; the
artifact store refuses unknown newer schema versions rather than downgrading them.
