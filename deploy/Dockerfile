# Ava bridge image — web app + API + dashboard + inference router.
# Multi-stage: build the React SPA, then run it on a slim Python base.
# Build context is the repo root:  docker build -f deploy/Dockerfile .

# ---- stage 1: build the frontend ----
# The Node major is app/frontend/.nvmrc's, and it is part of the build contract
# rather than a preference. `npm run build` runs app/frontend/scripts/ensure-deps.mjs
# first, which reads the .nvmrc that `COPY app/frontend/ ./` brings in (.dockerignore
# excludes app/frontend/node_modules and app/frontend/dist, not .nvmrc) and refuses a
# mismatched major — so this stage pinned to 20 while .nvmrc said 22 failed the
# image build outright, with a message about a dist diff nobody made. That
# message is correct: CI's frontend-dist-drift job byte-compares its own rebuild
# against the committed bundle, so the interpreter major decides whether an
# unrelated PR goes green.
#
# The number lives in this one ARG so the file has a single pin. It is still a
# COPY of a fact stored in app/frontend/.nvmrc, so tests/unit/test_node_version_ssot.py
# asserts the two agree — the same reason deploy/model-flags.conf has a guard.
ARG NODE_VERSION=22
FROM node:${NODE_VERSION}-slim AS web
WORKDIR /web
COPY app/frontend/package*.json ./
RUN npm ci
COPY app/frontend/ ./
# app/frontend/src/lib/chatEvents.contract.test.ts imports ../../../tests/integration/fakes/run-events.json
# and app/frontend/tsconfig.app.json includes all of src, so `tsc -b` needs the fixture at
# /tests/integration/fakes relative to WORKDIR /web or the stage fails with TS2307.
COPY tests/integration/fakes/run-events.json /tests/integration/fakes/run-events.json
RUN npm run build

# ---- stage 2: python runtime ----
FROM python:3.12-slim AS runtime
# Release CI passes these from the git tag so `ava version` / /api/health report
# the real build (see app/backend/version.py). Default to the committed VERSION.
ARG AVA_VERSION=""
ARG AVA_REVISION=""
# AVA_HOST=0.0.0.0 is REQUIRED here, and is not the security regression it looks
# like. The container's network namespace IS the boundary: compose publishes
# "127.0.0.1:8096:8096", so the port is reachable only from the host's loopback.
# Binding 127.0.0.1 *inside* the namespace instead means the published port has
# nothing listening behind it — every connection is refused while the container
# reports healthy, because the HEALTHCHECK below curls that same in-namespace
# loopback and succeeds. That combination shipped: `docker compose up` gave a
# green container and a browser that could not connect, after a multi-GB pull.
#
# Nothing else seeds it — .dockerignore excludes .env and ava.yaml, and /data is
# a fresh volume — so app/backend/config.py's 127.0.0.1 default was what applied.
#
# If you run this image with `--network host`, it binds every interface. That is
# the one mode where this matters; compose does not use it, and CI asserts so.
ENV PYTHONUNBUFFERED=1 \
    AVA_HOME=/data \
    AVA_HOST=0.0.0.0 \
    AVA_VERSION=${AVA_VERSION} \
    AVA_REVISION=${AVA_REVISION}
WORKDIR /app
# ffmpeg: decodes browser-mic recordings for voiceprint enrollment (Hub Voice
# tab). The heavy voice ML extras (torch/whisper/speechbrain) stay opt-in via
# AVA_VOICE_DEPS=1 to keep the default image slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*
ARG AVA_VOICE_DEPS=0
COPY config/dependencies/ ./config/dependencies/
RUN pip install --no-cache-dir --require-hashes -r config/dependencies/runtime.lock \
    && if [ "$AVA_VOICE_DEPS" = "1" ]; then \
         pip install --no-cache-dir -r config/dependencies/voice.txt \
           --extra-index-url https://download.pytorch.org/whl/cpu; \
       fi
COPY . /app
RUN pip install --no-cache-dir --no-deps -e .
COPY --from=web /web/dist /app/app/frontend/dist
RUN chmod -R a+rX /app
VOLUME ["/data"]
EXPOSE 8096
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8096/api/health || exit 1
# app/__main__.py binds host/port from config (AVA_HOST/AVA_PORT -> ava.yaml -> default).
CMD ["python", "-m", "app"]
