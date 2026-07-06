# Security Model — Ava

Ava is a personal, **on-premise** AI assistant running on a single host you
control. It handles sensitive data (login credentials, chat history, and
**biometric voiceprints**), so security is designed in, not bolted on. This
document is the human-readable companion to the two generated security diagrams:

- **Trust boundaries & control points** — [`agent/docs/diagrams/security.svg`](agent/docs/diagrams/security.svg)
- **Per-tool egress trace (least privilege)** — [`agent/docs/diagrams/policy.svg`](agent/docs/diagrams/policy.svg)

Both diagrams are **generated** from the SSOT manifest
([`agent/docs/architecture.yaml`](agent/docs/architecture.yaml)) and drift-checked
1:1, so they cannot silently fall out of date with reality.

## 1. Trust boundaries

| Boundary | Trust | What enforces it |
|----------|-------|------------------|
| 🌐 Internet / LAN | Untrusted | Nothing is exposed except via Tailscale |
| 🛡️ Perimeter | Authenticated | Tailscale TLS + the app **auth gate** (signed-cookie session) |
| 🔒 Host (loopback) | Trusted | App services bind `127.0.0.1` only; browser access goes through `tailscale serve`, and sandbox-only access goes through per-port gateway forwarders |
| 🔒 Sandbox (Docker) | Confined | The agent runs in an OpenClaw sandbox with **no ambient egress** — every outbound call passes the SSRF guard + per-tool allow-list |

## 2. Authentication & sessions (the perimeter)

The Experience app (`:8445`) is password-gated by middleware in
[`phone_bridge.py`](phone_bridge.py):

- Unauthenticated page requests → `303` redirect to `/login`; API/media requests → `401`.
- Public paths only: `/login`, `/logout`, `/api/health`, `/favicon.ico`.
- Session = **HMAC-SHA256 signed cookie** (`exp.sig`, cookie `ava_session`,
  `HttpOnly` + `Secure` + `SameSite=Lax`, 30-day TTL).
- Per-IP login **throttle** (8 attempts / 60 s).
- Password source: env `AVA_PASSWORD`, else generated to `data/auth_password` (`0600`).
- HMAC key source: env `AVA_SECRET`, else `data/.secret` (`0600`).

Tunables: `AVA_PASSWORD`, `AVA_SECRET`, `AVA_SESSION_TTL_DAYS`, `AVA_COOKIE_SECURE`.

## 3. Egress model — least privilege (the sandbox)

The agent cannot reach the network freely. **Every MCP tool is bound to its own
narrow egress policy**; anything not explicitly allowed is denied by default.
See [`agent/policies/`](agent/policies/) and the generated
[policy trace](agent/docs/diagrams/policy.svg).

| Policy | Tools | Allowed egress (and nothing else) |
|--------|-------|-----------------------------------|
| `ava-weather` | `get_weather` | `api.open-meteo.com:443` + `geocoding-api.open-meteo.com:443` — **GET** only |
| `ava-gpusvc` | `run_gpu_job` | `host.openshell.internal:8189` — GET·POST |
| `ava-knowledge` | document, image, media-content, web tools | `host.openshell.internal:8096` — enumerated `/internal/...` routes only + scoped `X-Ava-Internal-Token` |

Host-callbacks additionally require a scoped `X-Ava-Internal-Token` bearer.
Scopes are enforced in `ava_bridge/internal.py` and derived in `agent/install.sh`;
the raw root token is not accepted by default. Private host-gateway IPs are
reached only because they are explicitly allow-listed past the SSRF guard.

## 4. Secret inventory

All secrets live **outside** version control and are `chmod 0600`:

| File | Purpose |
|------|---------|
| `data/auth_password` | App login password (if `AVA_PASSWORD` unset) |
| `data/.secret` | HMAC key for signed session cookies |
| `data/.internal_token` | Root secret used to derive scoped sandbox→bridge callback bearers |
| `~/.config/tstruct/auth.json` | TALA layout-engine API token |

`.venv/`, `models/`, `media/`, `data/`, `bin/`, and `.env` are `.gitignore`d.
**Never** commit a secret; never log secret values.

## 5. Sensitive data handling

- **Voiceprints** (`models/voiceprint.npy`) are biometric data. They never leave
  the host and are excluded from git. The speaker gate (ECAPA-TDNN) compares an
  embedding locally; raw enrollment audio is not retained in the repo.
- **Chat history** (`data/chats.json`) and **uploads** (`media/uploads/`) stay on
  the host. The knowledge tools can read uploads only through enumerated,
  scoped, token-gated `/internal/...` routes.
- Inference is **local** (vLLM on-host); prompts and replies are not sent to any
  third-party model API.

## 6. Threat model (summary)

| Threat | Mitigation |
|--------|------------|
| Unauthorized app access | Tailscale-only exposure + signed-session auth gate + login throttle |
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

## 8. Reporting

This is a single-operator on-prem system. Security concerns or regressions are
tracked directly in the repo's commit history and (when applicable) recorded as an
ADR under [`agent/docs/adr/`](agent/docs/adr/). If Ava's own tooling can reach a
new network destination, that **must** be expressed as a new narrow policy in
`agent/policies/` and declared in the manifest — never as a broad allow-rule.
