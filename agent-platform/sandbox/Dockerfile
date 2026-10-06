# Sandbox image for tools marked `sandbox: required` (e.g. shell.run).
# Build:  docker build -t agent-sandbox:latest sandbox/
# The runtime adds the isolation flags from sandbox/profiles/<profile>.yaml at `docker run`
# time (no network, read-only root, dropped capabilities, non-root user, resource limits).
# Keep this image minimal: every tool you add here is a tool the agent can run.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 agent \
 && useradd --uid 10001 --gid agent --no-create-home --shell /usr/sbin/nologin agent

WORKDIR /workspace
USER 10001:10001
ENTRYPOINT []
