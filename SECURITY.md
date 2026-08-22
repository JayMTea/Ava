# Security model

## Is my data safe? The short answer

Ava runs on one machine that you control. Your chats, your uploaded files, your
long-term memory and your voiceprint are ordinary files on that machine's disk.
Out of the box the app answers only on the machine it runs on, behind a
password, and Ava's agent has no network access of its own - each of its tools
is allowed a short, named list of destinations and nothing else. Nothing is sent
to anyone else unless you switch that on yourself: a cloud model you configured,
a web search, or an API key you set.

The rest of this page is the detailed version, written for someone checking
those claims rather than taking them. Every control below names the file that
enforces it, so you can go and read it.

!!! note "Two words you will meet below"

    A **sandbox** is a locked-down container the agent runs inside: its own
    filesystem, and no way onto the network except the routes it is handed. An
    **egress policy** is that hand-out written down - a list of the exact hosts
    and paths one tool may reach, with everything else denied.

---

Ava is a personal, **on-premise** AI assistant running on a single host you
control. It handles sensitive data (login credentials, chat history, and
**biometric voiceprints**), so security is designed in, not bolted on. This
document is the human-readable companion to two diagrams:

- **Trust boundaries and control points**: [security diagram](agent/docs/diagrams/security.svg)
- **How the pieces fit together**: [architecture overview](docs/assets/architecture.svg)

??? note "How those diagrams stay true"

    The security diagram (and a per-tool egress/policy trace) is **generated**
    from a deployment-local SSOT manifest (`agent/docs/architecture.yaml`,
    gitignored; each install describes its own topology) and drift-checked 1:1,
    so it cannot silently fall out of date with reality. The architecture
    overview is a hand-authored, app-agnostic system map, not a generated
    artifact.

## 1. Trust boundaries

[![Trust zones from the internet down to the sandbox: an untrusted internet/LAN zone, a Tailscale TLS + auth-gate perimeter, a loopback-only host zone holding the bridge, the 0600 secrets and Tor-only web egress, and a Docker sandbox with no ambient egress that reaches the bridge only over enumerated /internal routes with a scoped token](agent/docs/diagrams/security.svg)](agent/docs/diagrams/security.svg)

| Boundary | Trust | What enforces it |
|----------|-------|------------------|
| Internet / LAN | Untrusted | The bridge binds loopback by default; any wider exposure (a VPN like Tailscale, or a reverse proxy) is the operator's explicit choice |
| Perimeter | Authenticated | The app **auth gate**: a signed-cookie session, on every request |
| Host (loopback) | Trusted | App services bind `127.0.0.1`; the inference router's `/v1` requires a bearer token when bound off-loopback; sandbox-only access goes through per-port gateway forwarders |
| Connector app (iframe) | **Trusted as the owner** | *Nothing* separates it from Ava. See the warning below. |
| Sandbox (Docker) | Confined | The agent runs in an OpenClaw sandbox with **no ambient egress**; every outbound call passes the SSRF guard (which blocks a tool being tricked into fetching an address it was never allowed) and a per-tool allow-list |

!!! warning "Enabling a connector app trusts its code as much as Ava's own"

    A connector app is reverse-proxied SAME-ORIGIN under `/apps/<id>/`, and the
    iframe keeps `allow-same-origin`, so the app's JavaScript can call any
    authenticated Ava API with your session - including approving Ava's own
    consent prompts. This is deliberate: it is what gives an embedded app single
    sign-on. It also means an embedded app is closer to a browser extension than
    to a browser tab. **Review a third-party app before you enable it.**

    Tightening the iframe sandbox would break the session inheritance the app
    proxy is built on. The real isolation control is a second **origin**
    (`apps.origin`), which is off by default - see
    [docs/CONNECTOR_SDK.md §3](docs/CONNECTOR_SDK.md).

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
  `/api/health`, `/favicon.ico`, plus `/manifest.webmanifest` and `/sw.js` -
  the PWA shell, which browsers fetch without credentials context and which
  must therefore not bounce to `/login`. Note `/setup/wizard` is *not* public.
  One further route skips the cookie gate entirely: the device-event ingest,
  `POST /api/connectors/<id>/events`, whose callers are third-party apps rather
  than browsers; it does its own per-connector bearer check instead.
- Session: an **HMAC-SHA256 signed cookie** (`exp.sid.sig`, cookie `ava_session`,
  `HttpOnly` + `SameSite=Lax`, 30-day TTL). The `sid` segment is what makes
  **revocation** possible: changing the password calls `auth.rotate_secret()`,
  which re-keys the HMAC and invalidates every session already issued - so
  "change my password" logs out a device you no longer hold, instead of only
  changing what the next login checks.
- **First-run claim gate.** `/setup` is public by necessity, so on a
  non-loopback bind anyone reaching the port could otherwise claim the admin
  password first. At boot with no password set, Ava writes a one-time token to
  `$AVA_HOME/data/setup_claim` (0600) and prints it. Setup is then allowed from
  loopback, or from anywhere with a matching `claim` - the Jupyter/Home
  Assistant pattern. The file is deleted on success.
- Per-IP login **throttle** (8 attempts / 60 s), keyed on the real client IP -
  behind a proxy this is the forwarded address, so one device cannot exhaust the
  whole LAN's bucket.
- Password source: env `AVA_PASSWORD`, else the first-run screen writes it
  `0600`, in **cleartext, not hashed** (§4).
- HMAC key source: env `AVA_SECRET`, else generated `0600` under `$AVA_HOME`.

??? note "The one case where a password change does not revoke sessions"

    When `AVA_SECRET` pins the key in the environment (the natural container
    setup, so sessions survive a recreate) `rotate_secret()` is a no-op, the
    response reports `revoked_other_sessions: false`, and outstanding cookies
    stay valid. On that deployment you revoke by rotating `AVA_SECRET` yourself.

??? note "Why the `Secure` cookie flag is resolved per request, not pinned"

    `Secure` is resolved **per request** (`auth.cookie_secure: auto`), not
    pinned at import. Pinning it on meant the browser silently discarded the
    cookie over plain HTTP, bouncing LAN and phone users back to `/login` with
    no error - indistinguishable from a wrong password, on exactly the flow
    [docs/MOBILE.md](docs/MOBILE.md) markets. Behind a proxy the scheme comes
    from `X-Forwarded-Proto`, believed only from peers in
    `server.trusted_proxies` (loopback by default).

    An outright refusal to serve `/setup` off loopback was rejected for the same
    class of reason: a headless box must stay claimable.

??? note "Environment tunables for the perimeter"

    | Variable | Sets |
    |---|---|
    | `AVA_PASSWORD` | The app login password |
    | `AVA_SECRET` | The HMAC key for session cookies |
    | `AVA_SESSION_TTL_DAYS` | Session lifetime (default 30) |
    | `AVA_COOKIE_SECURE` | Override the `auto` resolution above |
    | `AVA_TRUSTED_PROXIES` | Peers whose `X-Forwarded-*` headers are believed |

## 3. Egress model: least privilege (the sandbox)

*Egress* is anything leaving the machine. Here is the whole picture: what stays
put, and what leaves only because you switched it on.

[![What stays on your machine and what leaves only if you switch it on. Staying: your chats and history, what Ava remembers about you, your files and images, your voiceprint, the model weights, your connected apps' data, your secrets and API keys. Leaving only when switched on: a web search, your prompt to a cloud model you picked, a model download, and reaching Ava from your phone. Each of those switches is off or unset by default](docs/assets/egress.svg)](docs/assets/egress.svg)

The agent cannot reach the network freely. **Every MCP tool is bound to its own
narrow egress policy**; anything not explicitly allowed is denied by default.
See [`agent/policies/`](agent/policies/). A per-tool policy-trace diagram is
generated locally by `agent/docs/arch.py` on installs with the SSOT manifest.

!!! note "MCP"

    Model Context Protocol: the open standard for describing a tool to an AI
    agent (its name, its arguments, what it returns). Ava's own capabilities -
    weather, web search, reading your documents - are MCP tools, and each
    one carries its own egress policy.

| Policy | Tools | Allowed egress (and nothing else) |
|--------|-------|-----------------------------------|
| `ava-weather` | `get_weather` | `api.open-meteo.com:443` + `geocoding-api.open-meteo.com:443` (**GET** only) |
| `ava-knowledge` | document, media-content, web tools | `host.openshell.internal:8096`, enumerated `/internal/...` routes only, plus a scoped `X-Ava-Internal-Token` |

The property that matters: the `content` group holds the token for the MCP server
that runs `web_fetch`, which is the surface prompt injection actually arrives on.
It cannot reach `/internal/config`, `/internal/policies`, `/internal/logs` or
`/internal/perf`. `tests/test_internal_scopes.py` and `qa/test_10_security.py`
both assert that directly. It also cannot reach `/internal/code-change`, because
that route no longer exists: governed self-editing was removed in full, and
`tests/test_security.py::SelfEditingIsRemovedTests` pins every layer of its
absence so it cannot return one piece at a time.

??? note "How the scoped callback tokens are enforced"

    Host callbacks additionally require a scoped `X-Ava-Internal-Token` bearer,
    derived per capability group in `agent/install.sh`. Which group may call
    which `/internal/*` route is declared in `internal.ROUTE_SCOPES` +
    `security.INTERNAL_SCOPE_GROUPS`, and enforced **in the middleware**
    (`auth.auth_gate` → `internal.group_may`) rather than per handler - so a
    route added later is covered without its author opting in, and a route
    nobody has classified is refused for group tokens rather than left open. The
    root token passes everywhere; it is held by the owner and the CLI, not by
    the sandbox.

    Private host-gateway IPs are reached only because they are explicitly
    allow-listed past the SSRF guard.

??? note "Post-mortem: this control was documented before it was wired"

    Until 2026-07-27 the paragraph above described a control that existed but
    was not wired: 24 of 25 handlers passed no scope, so any valid group token
    reached every route. It is enforced now; the note stays because "documented,
    tested, and not enforced" is a failure mode worth naming.

## 4. Secret inventory

All secrets live **outside** version control and are `chmod 0600`. They resolve
from env first, else a generated file under `$AVA_HOME` (default the repo root;
`/data` in containers):

| Secret | Purpose |
|--------|---------|
| `data/auth_password` | App login password (if `AVA_PASSWORD` unset), stored `0600` in **cleartext, not hashed** - file-read access to `$AVA_HOME/data` is equivalent to knowing the password, so do not reuse a password you use elsewhere |
| `data/.secret` | HMAC key for signed session cookies (or `AVA_SECRET`) |
| `data/.internal_token` | Root secret that derives scoped sandbox→bridge callback bearers |
| `secrets/router_token` | Guards the inference router's control + LAN-exposed `/v1` |
| `secrets/inference_key` | Cloud-provider API key, when a cloud backend is used |
| `data/setup_claim` | One-time first-run claim token; deleted once setup completes |
| `secrets/env/<NAME>` | Connector credentials, keyed by the env-var name the connector's manifest declares (saved from Setup → Connectors; never written to a manifest or `ava.yaml`) |

**No agent tool can read any of these.** There is no longer a code tool loop to
gate: the agent's file-reading and file-writing tools were removed with
self-editing, so the deny-list they needed went with them. What Ava can read is
now enumerated positively — the `/internal/*` routes in §3, each scoped to a
capability group.

`.venv/`, `models/`, `media/`, `data/`, `secrets/`, `bin/piper/`, `ava.yaml`, and
`.env` are `.gitignore`d. **Never** commit a secret; never log secret values.

??? note "Post-mortem: the class of bug this removal retired"

    The code agent's deny-list was originally consulted only on writes, so a
    prompt-injected agent could `read_file(".env")` - which returned the
    Anthropic key - or, worse, `search("ANTHROPIC_API_KEY")`, whose own
    directory walk bypassed the path resolver entirely and returned the key
    inline in the match. It was fixed by moving the deny-list onto reads.

    It is recorded here because it is the argument for the removal rather than
    against the fix: a tool loop with arbitrary repo read/write needs a correct
    deny-list on every channel, forever, including channels added later. Not
    having the loop is a smaller thing to get right.

## 5. Sensitive data handling

- **Voiceprints** (`models/voiceprint.npy`) are biometric data. They never leave
  the host and are excluded from git. The speaker gate (ECAPA-TDNN) compares an
  embedding locally; raw enrollment audio is not retained in the repo. Enrollment
  and destruction are both recorded in the audit ledger with a content digest, so
  a deletion is provable without the artifact being retained - the written
  retention-and-destruction policy is [docs/BIOMETRICS.md](docs/BIOMETRICS.md)
  (GDPR Art. 9 special-category data; BIPA §15(a) asks for exactly that document).
  Delete it in **Setup → Agent → Voice**, or `POST /api/hub/voice/delete`.
- **Chat history** (`data/chats.db`) and **uploads** (`media/uploads/`) stay on
  the host. The knowledge tools can read uploads only through enumerated,
  scoped, token-gated `/internal/...` routes.
- Inference defaults to a **local** engine (vLLM/Ollama/llama.cpp on-host);
  chat prompts and replies are not sent to any third-party API unless you
  configure a cloud backend. That is now the only such switch: Ava has no
  third-party model API key of its own.

!!! note "Voice gate limitations (honest scope)"

    The speaker gate is a privacy and convenience **filter**, not an
    authentication factor. It is text-independent cosine matching with **no
    liveness or anti-spoofing detection**: a recording of the enrolled voice, or
    a good TTS clone of it, will pass. It also fails **open** when voice is
    enabled with no voiceprint enrolled (anyone can talk to the assistant until
    enrollment; the Setup hub warns about this state). Do not gate sensitive
    *actions* on the voice gate alone; the web session cookie remains the
    authentication boundary.

## 6. Threat model (summary)

| Threat | Mitigation |
|--------|------------|
| Unauthorized app access | Loopback-bound by default + signed-session auth gate + login throttle (private-network/VPN exposure is the operator's choice) |
| Compromised / prompt-injected tool | Per-tool egress allow-list (deny by default); blast radius limited to that tool's single destination |
| SSRF from a tool | Guard proxy rejects non-allow-listed IPs/hosts |
| Secret leakage | `0600` files, `.gitignore`, never logged |
| Secret exfiltration via an agent file tool | There is no agent file tool. Repo read/write went with self-editing; `tests/test_security.py::SelfEditingIsRemovedTests` keeps it gone |
| Admin takeover on first run | One-time claim token; `/setup` accepts loopback or a matching token, nothing else |
| Session theft / lost device | Password change re-keys the session HMAC, invalidating every issued cookie (a no-op when `AVA_SECRET` pins the key - rotate that instead) |
| A pasted connector command reading the bridge's env | Unsandboxed stdio children get a minimal env (a fixed allow-list - `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, `TERM`, `NODE_PATH`, `NVM_DIR`, `SYSTEMROOT` - plus the manifest's declared `env:`), never `os.environ`; probes default to a Docker sandbox and fail closed without it |
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

**How to read its output.** It has three channels, which are three different
claims:

- `- FAILED` lines are findings about Ava: a service of Ava's exposed wider than
  its bind class allows, an over-broad egress rule, or a secret file with loose
  permissions. Exit code 1.
- `! Declared exposures` are ports *you* have declared deliberately exposed in
  `ava.yaml` under `security.declared_exposures`, with your reason echoed back.
  They print on every run - declaring records a decision, it does not hide it.
- `~ Wildcard binds on ports Ava does not own` are listeners on this machine that
  are reachable from every network it joins but belong to something else. Not
  findings about Ava; reported because a tool that walks the whole socket table
  and then says nothing about them would be worse than one that admits its scope.

Bind classes: loopback and the RFC1918 sandbox gateway pass. A **Tailscale
(CGNAT) bind** is the operator's explicit choice per §2, so it passes *when
declared* and fails otherwise. A **wildcard bind** (`0.0.0.0`/`::`/`*`) on one of
Ava's ports always fails and cannot be declared away.

??? note "Scope caveats, so you do not over-read a clean run"

    - The port check reads the **live socket table**, so its result is a fact
      about the machine it ran on, not about the code. It cannot pass or fail in
      CI meaningfully; `tests/test_port_exposure_classes.py` tests the *rules*
      against injected listener tables instead.
    - `SENSITIVE_PORTS` is an enumeration of Ava's own services. Only the
      wildcard-bind advisory covers everything else, so a service of yours on an
      unlisted port bound to a *routable* address is not reported. Add its port.
    - `ss -tlnH` is TCP listeners only: no UDP, no unix sockets.

## 8. Reporting a vulnerability

Please report security issues **privately**, not in public issues or PRs:

- Preferred: GitHub **Private vulnerability reporting** -
  [open an advisory](https://github.com/JayMTea/Ava/security/advisories/new), or
  repo → Security → *Report a vulnerability*. That opens a private thread visible
  only to you and the maintainer.
- If that form is unavailable to you for any reason, contact the maintainer
  through the [GitHub profile](https://github.com/JayMTea) instead. There is
  deliberately no published email address: an inbox in a public repository gets
  scraped, and a report sent to a scraped address is a report that competes with
  spam. The advisory thread is both more private and more reliable.
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

The `agent` profile uses a second signed image,
`ghcr.io/jaymtea/ava-agent-runtime:X.Y.Z` - verify it exactly the same way, then
set it as the `agent` service's `image:` in `deploy/docker-compose.override.yml`
(`deploy/docker-compose.yml` still ships the local build tag
`ava/agent-runtime:latest`).

??? note "SBOM, provenance, and the tag signature"

    The build also attaches an **SBOM** (a machine-readable inventory of what
    went into the image) and **SLSA provenance** attestation to the image index.
    These are BuildKit in-toto attestation manifests, not cosign attestations,
    so `cosign verify-attestation` will not find them - inspect them directly:

    ```bash
    docker buildx imagetools inspect ghcr.io/jaymtea/ava-bridge:X.Y.Z   # shows arches + attestations
    ```

    Each GitHub Release additionally ships a source SBOM
    (`ava-sbom.cyclonedx.json`) and `checksums.txt`. Release tags are intended
    to be cut with `git tag -s`, but the workflow only warns on an unsigned tag
    rather than blocking the release - so verify the tag signature yourself if
    that matters to you. See [docs/RELEASING.md](docs/RELEASING.md).
