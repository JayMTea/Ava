# deploy/ — how this platform runs outside a laptop

Empty in the template on purpose: deployment is project-specific. Put here whatever turns
`app/` into a running service — Dockerfile, compose file, Kubernetes manifests, systemd
units, CI/CD deploy jobs.

## Checklist for any deployment

- **Secrets** come from the environment or a secret manager, never from files in the image.
  `.env` is for local development only.
- **`state/` is a volume.** Sessions, tasks, checkpoints and memory must survive restarts and
  must not be baked into images. Back it up if memory matters.
- **Approvals** have a real channel (UI, chat, ticket) or `non_interactive: deny` stays on.
- **Sandbox:** if any tool needs `mode: docker`, the host needs Docker and the
  `agent-sandbox` image (`docker build -t agent-sandbox:latest sandbox/`).
- **Egress:** model-provider endpoints must be reachable; `policies/network.yaml` governs
  only what tools fetch.
- **Observability:** set `observability/tracing.yaml` `exporter: otlp` and
  `OTEL_EXPORTER_OTLP_ENDPOINT` so traces leave the box.
- **Gate releases on evals:** `python -m evals.run` must pass before deploy.
