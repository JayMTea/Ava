# Security model

Ava is a personal, **on-premise** AI assistant running on a single host you
control. It handles sensitive data (login credentials, chat history, and
**biometric voiceprints**), so security is designed in, not bolted on. This
document is the human-readable companion to two diagrams:

- **Trust boundaries and control points**: [security diagram](agent/docs/diagrams/security.svg)
- **How the pieces fit together**: [architecture overview](docs/assets/architecture.svg)

The security diagram (and a per-tool egress/policy trace) is **generated** from
a deployment-local SSOT manifest (`agent/docs/architecture.yaml`, gitignored;
each install describes its own topology) and drift-checked 1:1, so it cannot
silently fall out of date with reality. The architecture overview is a
hand-authored, app-agnostic system map, not a generated artifact.

## 1. Trust boundaries

| Boundary | Trust | What enforces it |
|----------|-------|------------------|
| Internet / LAN | Untrusted | The bridge binds loopback by default; any wider exposure (a VPN like Tailscale, or a reverse proxy) is the operator's explicit choice |
| Perimeter | Authenticated | The app **auth gate**: a signed-cookie session, on every request |
| Host (loopback) | Trusted | App services bind `127.0.0.1`; the inference router's `/v1` requires a bearer token when bound off-loopback; sandbox-only access goes through per-port gateway forwarders |
| Connector app (iframe) | **Trusted as the owner** | *Nothing.* The app is reverse-proxied SAME-ORIGIN under `/apps/<id>/`, and the iframe keeps `allow-same-origin`, so its JavaScript can call any authenticated Ava API with your session. This is deliberate — it is what gives an embedded app single sign-on — but it means **enabling a connector app is trusting its code as much as Ava's own**. Review a third-party app before enabling it. Tightening the sandbox would break the session inheritance the app proxy is built on; see docs/CONNECTOR_SDK.md §3. |
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
- Public paths (exact match, no prefixes): `/login`, `/logout`, `/setup`,
  `/api/health`, `/favicon.ico`, plus `/manifest.webmanifest` and `/sw.js` —
  the PWA shell, which browsers fetch without credentials context and which
  must therefore not bounce to `/login`. Note `/setup/wizard` is *not* public.
  One further route skips the cookie gate entirely: the device-event ingest,
  `POST /api/connectors/<id>/events`, whose callers are third-party apps rather
  than browsers; it does its own per-connector bearer check instead.
- Session: an **HMAC-SHA256 signed cookie** (`exp.sid.sig`, cookie `ava_session`,
  `HttpOnly` + `SameSite=Lax`, 30-day TTL). The `sid` segment is what makes
  **revocation** possible: changing the password calls `auth.rotate_secret()`,
  which re-keys the HMAC and invalidates every session already issued — so
  "change my password" logs out a device you no longer hold, instead of only
  changing what the next login checks. One exception: when `AVA_SECRET` pins the
  key in the environment (the natural container setup, so sessions survive a
  recreate) `rotate_secret()` is a no-op, the response reports
  `revoked_other_sessions: false`, and outstanding cookies stay valid — on that
  deployment you revoke by rotating `AVA_SECRET` yourself.
- `Secure` is resolved **per request** (`auth.cookie_secure: auto`), not pinned
  at import. Pinning it on meant the browser silently discarded the cookie over
  plain HTTP, bouncing LAN and phone users back to `/login` with no error —
  indistinguishable from a wrong password, on exactly the flow docs/MOBILE.md
  markets. Behind a proxy the scheme comes from `X-Forwarded-Proto`, believed
  only from peers in `server.trusted_proxies` (loopback by default).
- **First-run claim gate.** `/setup` is public by necessity, so on a
  non-loopback bind anyone reaching the port could otherwise claim the admin
  password first. At boot with no password set, Ava writes a one-time token to
  `$AVA_HOME/data/setup_claim` (0600) and prints it. Setup is then allowed from
  loopback, or from anywhere with a matching `claim` — the Jupyter/Home
  Assistant pattern. The file is deleted on success. An outright refusal was
  rejected because a headless box must stay claimable.
- Per-IP login **throttle** (8 attempts / 60 s), keyed on the real client IP —
  behind a proxy this is the forwarded address, so one device cannot exhaust the
  whole LAN's bucket.
- Password source: env `AVA_PASSWORD`, else the first-run screen writes it `0600`
  — in **cleartext, not hashed** (§4).
- HMAC key source: env `AVA_SECRET`, else generated `0600` under `$AVA_HOME`.

Tunables: `AVA_PASSWORD`, `AVA_SECRET`, `AVA_SESSION_TTL_DAYS`, `AVA_COOKIE_SECURE`,
`AVA_TRUSTED_PROXIES`.

## 3. Egress model: least privilege (the sandbox)

The agent cannot reach the network freely. **Every MCP tool is bound to its own
narrow egress policy**; anything not explicitly allowed is denied by default.
See [`agent/policies/`](agent/policies/). A per-tool policy-trace diagram is
generated locally by `agent/docs/arch.py` on installs with the SSOT manifest.

| Policy | Tools | Allowed egress (and nothing else) |
|--------|-------|-----------------------------------|
| `ava-weather` | `get_weather` | `api.open-meteo.com:443` + `geocoding-api.open-meteo.com:443` (**GET** only) |
| `ava-knowledge` | document, image (`run_gpu_job`), media-content, web tools | `host.openshell.internal:8096`, enumerated `/internal/...` routes only, plus a scoped `X-Ava-Internal-Token` |

GPU workloads rides that same policy: the sandbox never reaches the GPU service
itself. `run_gpu_job` calls `POST /internal/run-gpu-job`, and the bridge
owns the render host-side.

Host callbacks additionally require a scoped `X-Ava-Internal-Token` bearer,
derived per capability group in `agent/install.sh`. Which group may call which
`/internal/*` route is declared in `internal.ROUTE_SCOPES` +
`security.INTERNAL_SCOPE_GROUPS`, and enforced **in the middleware**
(`auth.auth_gate` → `internal.group_may`) rather than per handler — so a route
added later is covered without its author opting in, and a route nobody has
classified is refused for group tokens rather than left open. The root token
passes everywhere; it is held by the owner and the CLI, not by the sandbox.

The property that matters: the `content` group holds the token for the MCP server
that runs `web_fetch`, which is the surface prompt injection actually arrives on.
It cannot reach `/internal/code-change`, `/internal/config`, `/internal/policies`,
`/internal/logs` or `/internal/perf`. `tests/test_internal_scopes.py` and
`qa/test_10_security.py` both assert that directly.

> Until 2026-07-27 this paragraph described a control that existed but was not
> wired: 24 of 25 handlers passed no scope, so any valid group token reached
> every route. It is enforced now; the note stays because "documented, tested,
> and not enforced" is a failure mode worth naming.

Private host-gateway IPs are reached only because they are explicitly
allow-listed past the SSRF guard.

## 4. Secret inventory

All secrets live **outside** version control and are `chmod 0600`. They resolve
from env first, else a generated file under `$AVA_HOME` (default the repo root;
`/data` in containers):

| Secret | Purpose |
|--------|---------|
| `data/auth_password` | App login password (if `AVA_PASSWORD` unset), stored `0600` in **cleartext, not hashed** — file-read access to `$AVA_HOME/data` is equivalent to knowing the password, so do not reuse a password you use elsewhere |
| `data/.secret` | HMAC key for signed session cookies (or `AVA_SECRET`) |
| `data/.internal_token` | Root secret that derives scoped sandbox→bridge callback bearers |
| `secrets/router_token` | Guards the inference router's control + LAN-exposed `/v1` |
| `secrets/inference_key` | Cloud-provider API key, when a cloud backend is used |
| `data/setup_claim` | One-time first-run claim token; deleted once setup completes |
| `secrets/env/<NAME>` | Connector credentials, keyed by the env-var name the connector's manifest declares (saved from Setup → Connectors; never written to a manifest or `ava.yaml`) |

The **code-change agent cannot read any of these**. `access_policy` was
originally consulted only on writes, so a prompt-injected agent could
`read_file(".env")` — which returned `ANTHROPIC_API_KEY` — or, worse, use
`search("ANTHROPIC_API_KEY")`, whose own directory walk bypassed the path
resolver entirely and returned the key inline in the match. The deny-list is now
enforced in `coder._safe()`, which every tool routes through for path resolution,
so a tool added later inherits the gate; `search` and `list_dir` restate it
because they walk the tree themselves.

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
  chat prompts and replies are not sent to any third-party API unless you
  configure a cloud backend — or set `ANTHROPIC_API_KEY`, which is a separate
  path with its own bullet below.
- **`ANTHROPIC_API_KEY` is an opt-in third-party egress**, distinct from
  configuring a cloud inference backend (§4). It ships empty in `.env.example`;
  once you set it, two paths leave the host. Governed code changes
  (`coder`/`code_agent`) post the prompt plus the contents of the repository
  files the tool loop reads to `https://api.anthropic.com/v1/messages` (model
  `AVA_CODE_MODEL`, default `claude-sonnet-4-6`). The learning /
  memory-distillation cycle falls back to the same API when the local router
  returns nothing, and its prompt carries short excerpts of your chat messages.
  Leave the key unset and neither can fire; with it set, the `access_policy`
  deny-list (§4) still keeps `.env`, `secrets/`, and `models/` out of what the
  code agent can read.

## 6. Threat model (summary)

| Threat | Mitigation |
|--------|------------|
| Unauthorized app access | Loopback-bound by default + signed-session auth gate + login throttle (private-network/VPN exposure is the operator's choice) |
| Compromised / prompt-injected tool | Per-tool egress allow-list (deny by default); blast radius limited to that tool's single destination |
| SSRF from a tool | Guard proxy rejects non-allow-listed IPs/hosts |
| Secret leakage | `0600` files, `.gitignore`, never logged |
| Secret exfiltration via the code agent | `access_policy` deny-list enforced on **reads** in `coder._safe()`, and restated in the two tools that walk the tree themselves |
| Admin takeover on first run | One-time claim token; `/setup` accepts loopback or a matching token, nothing else |
| Session theft / lost device | Password change re-keys the session HMAC, invalidating every issued cookie (a no-op when `AVA_SECRET` pins the key — rotate that instead) |
| A pasted connector command reading the bridge's env | Unsandboxed stdio children get a minimal env (a fixed allow-list — `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, `TERM`, `NODE_PATH`, `NVM_DIR`, `SYSTEMROOT` — plus the manifest's declared `env:`), never `os.environ`; probes default to a Docker sandbox and fail closed without it |
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

Two scope caveats, so you read its output correctly. The policy scan globs
`agent/policies/*.yaml` at the top level only, so the connector-derived policies
under `agent/policies/generated/` — the ones you get by connecting an app — are
not covered; review those by hand. And `ava-weather.yaml` is a tracked, known
hit for the `/**` rule, so a clean clone reports that finding out of the box:
this is a review aid, not a gate that currently passes.

## 8. Reporting a vulnerability

Please report security issues **privately**, not in public issues or PRs:

- Preferred, once the repository is public: GitHub **Private vulnerability
  reporting** (repo → Security → *Report a vulnerability*), which opens a
  private advisory thread.
- While the repository is private that Security tab is not reachable, so there
  is no advisory form to file against. Contact the maintainer directly through
  the [GitHub profile](https://github.com/JayMTea) instead.
- Include: affected version (`ava version`), a description, and reproduction steps.
- Expect an acknowledgement within a few days. Coordinated disclosure: we will
  agree on a fix and disclosure timeline before any public write-up.

Internal rule (contributors): if Ava's tooling can reach a new network
destination, that **must** be a new narrow policy in `agent/policies/` declared in
the manifest, never a broad allow-rule. Significant security decisions are
recorded as an ADR under [`agent/docs/adr/`](agent/docs/adr/).

## 9. Verifying a release

From the first published release onward, images are signed **keylessly with
cosign** (Sigstore). The signature's identity is the GitHub Actions release
workflow, so you can prove an image came from this repo's CI and wasn't tampered
with. Verify before running:

```bash
# X.Y.Z = the release version. The git tag is vX.Y.Z; the published image tag
# drops the v. (For a fork, swap in your own owner/repo — note the registry path
# is lowercased, while the cert-identity keeps the repo's case.)
cosign verify ghcr.io/jaymtea/ava-bridge:X.Y.Z \
  --certificate-identity-regexp "https://github.com/JayMTea/.+/.github/workflows/release.yml@refs/tags/vX.Y.Z" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The `agent` and `full` profiles use a second signed image,
`ghcr.io/jaymtea/ava-agent-runtime:X.Y.Z` — verify it exactly the same way, then
set it as the `agent` service's `image:` in `deploy/docker-compose.override.yml`
(`deploy/docker-compose.yml` still ships the local build tag
`ava/agent-runtime:latest`).

The build also attaches an **SBOM** and **SLSA provenance** attestation to the
image index. These are BuildKit in-toto attestation manifests, not cosign
attestations, so `cosign verify-attestation` will not find them — inspect them
directly:

```bash
docker buildx imagetools inspect ghcr.io/jaymtea/ava-bridge:X.Y.Z   # shows arches + attestations
```

Each GitHub Release additionally ships a source SBOM (`ava-sbom.cyclonedx.json`)
and `checksums.txt`. Release tags are intended to be cut with `git tag -s`, but
the workflow only warns on an unsigned tag rather than blocking the release — so
verify the tag signature yourself if that matters to you. See
[docs/RELEASING.md](docs/RELEASING.md).
