# Ava agent-runtime image — runs NemoClaw + the RemoteRuntime shim. It owns the
# Docker socket (mounted from the host) so OpenShell can spawn the agent sandbox;
# the bridge container stays clean and talks to this over HTTP.
#   docker build -f deploy/agent.Dockerfile -t ava/agent-runtime .
FROM python:3.12-slim AS agent

# NemoClaw installs into $HOME (.local/bin + .npm-global/bin); put those on PATH.
ENV PYTHONUNBUFFERED=1 \
    AVA_HOME=/data \
    NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1 \
    PATH="/root/.local/bin:/root/.npm-global/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
ARG DOCKER_CLI_VERSION=29.6.1
# The NemoClaw ref the entrypoint installs from. This was `NEMOCLAW_INSTALL_TAG`,
# which nothing read — the entrypoint has always looked for NEMOCLAW_INSTALL_REF,
# and with the ARG never plumbed through it fell back to piping the upstream
# `main` branch into bash at every first container start. Pinned and exported so
# the same image installs the same agent runtime twice running.
#
# Moving it: check https://github.com/NVIDIA/NemoClaw/tags, bump, rebuild.
# `NEMOCLAW_INSTALL_REF=main` still works as an escape hatch if a pin goes bad.
# Pinned 2026-07-27.
ARG NEMOCLAW_INSTALL_REF=v0.0.96
ENV NEMOCLAW_INSTALL_REF=${NEMOCLAW_INSTALL_REF}

# NOTES (each learned by testing the real thing):
#  * the npm `nemoclaw` package is an empty STUB — the real CLI comes from
#    NVIDIA's official installer, which needs a REACHABLE Docker daemon, so it
#    runs at container start (the socket is mounted then), NOT at build.
#  * NemoClaw requires Node >=22.16.
#  * `docker.io` on slim ships only the daemon; we drop in the static Docker
#    CLIENT (OpenShell talks to the mounted host daemon).
# So this stage installs only the toolchain; the entrypoint installs NemoClaw.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       curl ca-certificates gnupg git python3 make g++ \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && case "$(dpkg --print-architecture)" in \
         amd64) DA=x86_64 ;; arm64) DA=aarch64 ;; *) DA="$(uname -m)" ;; esac \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${DA}/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar xz -C /usr/local/bin --strip-components=1 docker/docker \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
COPY deploy/agent-entrypoint.sh /usr/local/bin/agent-entrypoint.sh
RUN chmod +x /usr/local/bin/agent-entrypoint.sh

EXPOSE 9100
# The shim is auth-gated (X-Ava-Agent-Token); it is not published to the host.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=10 \
    CMD curl -fsS http://127.0.0.1:9100/healthz || exit 1
ENTRYPOINT ["/usr/local/bin/agent-entrypoint.sh"]
