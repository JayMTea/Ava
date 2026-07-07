# Ava bridge image — web app + API + dashboard + inference router.
# Multi-stage: build the React SPA, then run it on a slim Python base.
# Build context is the repo root:  docker build -f deploy/Dockerfile .

# ---- stage 1: build the frontend ----
FROM node:20-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# ---- stage 2: python runtime ----
FROM python:3.12-slim AS runtime
# Release CI passes these from the git tag so `ava version` / /api/health report
# the real build (see ava_bridge/version.py). Default to the committed VERSION.
ARG AVA_VERSION=""
ARG AVA_REVISION=""
ENV PYTHONUNBUFFERED=1 \
    AVA_HOME=/data \
    AVA_VERSION=${AVA_VERSION} \
    AVA_REVISION=${AVA_REVISION}
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
COPY --from=web /web/dist /app/frontend/dist
VOLUME ["/data"]
EXPOSE 8096
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8096/api/health || exit 1
# serve.py binds host/port from config (AVA_HOST/AVA_PORT -> ava.yaml -> default).
CMD ["python", "serve.py"]
