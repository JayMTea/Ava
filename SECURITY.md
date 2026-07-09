# Security model

Ava is a personal, **on-premise** AI assistant running on a single host you
control. It handles sensitive data (login credentials, chat history, and
**biometric voiceprints**), so security is designed in, not bolted on. This
document is the human-readable companion to the generated security diagram:

- **Trust boundaries and control points**: [architecture overview](docs/assets/architecture.svg)

The diagram (and a per-tool egress/policy trace) is **generated** from a
deployment-local SSOT manifest (`agent/docs/architecture.yaml`, gitignored;
each install describes its own topology) and drift-checked 1:1, so it cannot
silently fall out of date with reality.

## 1. Trust boundaries

| Boundary | Trust | What enforces it |
|----------|-------|------------------|
| Internet / LAN | Untrusted | The bridge binds loopback by default; any wider exposure (a VPN like Tailscale, or a reverse proxy) is the operator's explicit choice |
| Perimeter | Authenticated | The app **auth gate**: a signed-cookie session, on every request |
| Host (loopback) | Trusted | App services bind `127.0.0.1`; the inference router's `/v1` requires a bearer token when bound off-loopback; sandbox-only access goes through per-port gateway forwarders |
| Sandbox (Docker) | Confined | The agent runs in an OpenClaw sandbox with **no ambient egress**; every outbound call passes the SSRF guard and a per-tool allow-list |

### Using Ava away from home: what Tailscale is, in plain terms

Out of the box, Ava only answers on the machine it runs on. That is the safest
default, but it also means your phone can't reach it from the couch or from
outside the house. The wrong fix is "opening a port" on your router, which puts
your assistant on the public internet where anyone can find and probe it.

[Tailscale](https://tailscale.com) is the easy, safe fix. It creates a small
private network (a "tailnet") between only the devices you sign in on: your
computer, your phone, your laptop. To each other they appear as if they were on
the same home network, no matter where you are; to everyone else they are
invisible. Nothing is exposed to the public internet, there is no port
forwarding, and the free personal plan covers this use.

In practice: install Tailscale on the machine running Ava and on your phone,
sign both into the same account, and run one command on the Ava machine
(`tailscale serve --bg 8096`). Ava is then reachable from your devices at a
private `https://…ts.net` address with a valid certificate, which also unlocks
the [install-to-home-screen experience](docs/MOBILE.md) on mobile. Your login
password still applies on every request; Tailscale controls who can *reach* the
door, and Ava's auth gate controls who can *open* it.

## 2. Authentication and sessions (the perimeter)

The bridge (`:8096`) is password-gated by middleware in `ava_bridge/auth.py`:

- Unauthenticated page requests get a `303` redirect to `/login` (or `/setup` on
  a fresh install); API/media/app requests get a `401` JSON response.
- Public paths only: `/login`, `/logout`, `/setup`, `/api/health`, `/favicon.ico`.
- Session: an **HMAC-SHA256 signed cookie** (`exp.sig`, cookie `ava_session`,
  `HttpOnly` + `Secure` + `SameSite=Lax`, 30-day TTL).
- Per-IP login **throttle** (8 attempts / 60 s).
- Password source: env `AVA_PASSWORD`, else the first-run screen writes it `0600`.
- HMAC key source: env `AVA_SECRET`, else generated `0600` under `$AVA_HOME`.

Tunables: `AVA_PASSWORD`, `AVA_SECRET`, `AVA_SESSION_TTL_DAYS`, `AVA_COOKIE_SECURE`.

## 3. Egress model: least privilege (the sandbox)

The agent cannot reach the network freely. **Every MCP tool is bound to its own
narrow egress policy**; anything not explicitly allowed is denied by default.
See [`agent/policies/`](agent/policies/). A per-tool policy-trace diagram is
generated locally by `agent/docs/arch.py` on installs with the SSOT manifest.

| Policy | Tools | Allowed egress (and nothing else) |
|--------|-------|-----------------------------------|
| `ava-weather` | `get_weather` | `api.open-meteo.com:443` + `geocoding-api.open-meteo.com:443` (**GET** only) |
| `ava-gpusvc` | `run_gpu_job` | `host.openshell.internal:8189` (GET and POST) |
| `ava-knowledge` | document, image, media-content, web tools | `host.openshell.internal:8096`, enumerated `/internal/...` routes only, plus a scoped `X-Ava-Internal-Token` |

Host callbacks additionally require a scoped `X-Ava-Internal-Token` bearer.
Scopes are enforced in `ava_bridge/internal.py` and derived in `agent/install.sh`;
the raw root token is not accepted by default. Private host-gateway IPs are
reached only because they are explicitly allow-listed past the SSRF guard.

## 4. Secret inventory

All secrets live **outside** version control and are `chmod 0600`. They resolve
from env first, else a generated file under `$AVA_HOME` (default the repo root;
`/data` in containers):

| Secret | Purpose |
|--------|---------|
| `data/auth_password` | App login password (if `AVA_PASSWORD` unset) |
| `data/.secret` | HMAC key for signed session cookies (or `AVA_SECRET`) |
| `data/.internal_token` | Root secret that derives scoped sandbox→bridge callback bearers |
| `secrets/router_token` | Guards the inference router's control + LAN-exposed `/v1` |
| `secrets/inference_key` | Cloud-provider API key, when a cloud backend is used |

`.venv/`, `models/`, `media/`, `data/`, `secrets/`, `bin/piper/`, `ava.yaml`, and
`.env` are `.gitignore`d. **Never** commit a secret; never log secret values.

## 5. Sensitive data handling

- **Voiceprints** (`models/voiceprint.npy`) are biometric data. They never leave
  the host and are excluded from git. The speaker gate (ECAPA-TDNN) compares an
  embedding locally; raw enrollment audio is not retained in the repo.
- **Voice gate limitations (honest scope):** the speaker gate is a privacy and
  convenience **filter**, not an authentication factor. It is text-independent
  cosine matching with **no liveness or anti-spoofing detection**: a recording
  of the enrolled voice, or a good TTS clone of it, will pass. It also fails
  **open** when voice is enabled with no voiceprint enrolled (anyone can talk to
  the assistant until enrollment; the Setup hub warns about this state). Do not
  gate sensitive *actions* on the voice gate alone; the web session cookie
  remains the authentication boundary.
- **Chat history** (`data/chats.json`) and **uploads** (`media/uploads/`) stay on
  the host. The knowledge tools can read uploads only through enumerated,
  scoped, token-gated `/internal/...` routes.
- Inference defaults to a **local** engine (vLLM/Ollama/llama.cpp on-host);
  prompts and replies are not sent to any third-party API unless you configure a
  cloud backend.

## 6. Threat model (summary)

| Threat | Mitigation |
|--------|------------|
| Unauthorized app access | Loopback-bound by default + signed-session auth gate + login throttle (private-network/VPN exposure is the operator's choice) |
| Compromised / prompt-injected tool | Per-tool egress allow-list (deny by default); blast radius limited to that tool's single destination |
| SSRF from a tool | Guard proxy rejects non-allow-listed IPs/hosts |
| Secret leakage | `0600` files, `.gitignore`, never logged |
| Biometric/PII exfiltration | Local-only storage, git-excluded, no external inference |

## 7. Security regression checks

Run the smoke check after adding services, ports, MCP tools, policies, or proxy
routes:

```bash
./ava_security_check.py
```

It fails on wildcard `/internal/**` policies, wildcard `/**` egress,
secret files with group/world permissions, and sensitive ports bound to wildcard
interfaces. New host-local services should bind `127.0.0.1` and, if the sandbox
must reach them, get a dedicated `*-gw.service` using `ava_bridge/gw_forward.py`.

## 8. Reporting a vulnerability

Please report security issues **privately**, not in public issues or PRs:

- Preferred: GitHub **Private vulnerability reporting** (repo → Security → *Report
  a vulnerability*), which opens a private advisory thread.
- Include: affected version (`ava version`), a description, and reproduction steps.
- Expect an acknowledgement within a few days. Coordinated disclosure: we will
  agree on a fix and disclosure timeline before any public write-up.

Internal rule (contributors): if Ava's tooling can reach a new network
destination, that **must** be a new narrow policy in `agent/policies/` declared in
the manifest, never a broad allow-rule. Significant security decisions are
recorded as an ADR under [`agent/docs/adr/`](agent/docs/adr/).

## 9. Verifying a release

Published images are signed **keylessly with cosign** (Sigstore). The signature's
identity is the GitHub Actions release workflow, so you can prove an image came
from this repo's CI and wasn't tampered with. Verify before running:

```bash
# vX.Y.Z = the release tag. (For a fork, swap in your own owner/repo — note the
# registry path is lowercased, while the cert-identity keeps the repo's case.)
cosign verify ghcr.io/jaymtea/ava-bridge:vX.Y.Z \
  --certificate-identity-regexp "https://github.com/JayMTea/.+/.github/workflows/release.yml@refs/tags/vX.Y.Z" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The build also attaches an **SBOM** and **SLSA provenance** attestation:

```bash
cosign verify-attestation --type spdxjson ghcr.io/jaymtea/ava-bridge:vX.Y.Z ...
docker buildx imagetools inspect ghcr.io/jaymtea/ava-bridge:vX.Y.Z   # shows arches + attestations
```

Each GitHub Release additionally ships a source SBOM (`ava-sbom.cyclonedx.json`)
and `checksums.txt`. Release tags are cut with `git tag -s` (signed); see
[docs/RELEASING.md](docs/RELEASING.md).
