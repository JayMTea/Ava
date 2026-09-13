# Optional deployment and data export

Ava can run on any supported Linux host. It has no dependency on a particular NAS,
warehouse, object store, exporter, hostname, or another application repository.

The standard Compose files do not load this directory. To add an exporter, choose
a compatible image, copy `shipper.yaml` outside the checkout, configure verified
TLS endpoints, and explicitly select the streams you want to export. The example
exports nothing. Supply exporter-only credentials in a separate owner-readable
file. Do not give the exporter Ava's deployment environment or signing secret.

Set `AVA_EXPORTER_IMAGE` (prefer an immutable digest), `AVA_EXPORTER_CONFIG`,
`AVA_EXPORTER_ENV`, `AVA_EXPORTER_STATE`, and `AVA_HOME` in your deployment config.
Set `AVA_EXPORTER_USER` to the UID/GID permitted to read your selected source files.
Include `nas/shipper.compose.yml` with Compose's `-f` or `COMPOSE_FILE`. Extend it
with your own networks and health check if your exporter needs them.

## Existing deployments

Deployment automation deliberately preserves the receiver's `deploy/nas/` files.
Existing custom export mappings, networks, paths, and credentials stay under the
operator's control. Copy your deployment files to a private backup before adopting
new templates. Changing this example does not migrate an existing exporter.

The optional CD workflow requires repository variables `NAS_DEPLOY=true` and
`NAS_STACK_DIR` set to the absolute existing checkout directory. The default runner
labels are `self-hosted,nas`; override `AVA_DEPLOY_RUNNER_LABELS` with a JSON list.
Both automatic and manual deployment require successful push CI for the selected
commit. Instance data, deployment overrides and private overlays are excluded from
synchronization. The workflow retains previous locally built images for rollback.
