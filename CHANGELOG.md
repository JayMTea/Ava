# Changelog

All notable changes to Ava are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Entries from 0.1.0
onward are published, signed releases; the dated sections below it are the
pre-release milestones from when Ava ran on one box and nothing was tagged.

## [Unreleased]

### Changed

- **Setup went from eleven tabs to six, and every surface now has one home.**
  Persona, Voice and Memory sat in the tab bar as peers of Agent even though
  they *are* the agent; they are now its sub-tabs, alongside Runtime, Brain and
  Skills. Budgets merged into Hardware (a spend cap reads better beside the
  machine that spends it) and History moved to the Data page. `AgentPanel.tsx`
  was 1,174 lines stacking five unrelated sections in one ~3,000px scroll — it
  is now a ~55-line sub-tab router over `AgentRuntimePanel` / `BrainPanel` /
  `ModelStorePanel` / `SkillsPanel` and the three panels that moved in.
  - **Memory had been rendering twice.** The same `MemoryPanel`, with the same
    props and the same data, was mounted under Setup *and* as a Data tab, so
    nothing told you which was authoritative. It lives at **Setup → Agent →
    Memory**; the Data page keeps the store-level facts and operations (size,
    count, export, empty) and its row links out, labelled "Browse in Setup" so
    it never reads as a local tab.
  - **So had the audit ledger.** Setup → History and Data → Logs → "Audit" were
    two renderings of one file — one humanised, one a raw `key=value` dump —
    each footer pointing at the other as the real home. History is now a Data
    tab and Logs no longer carries an `audit` source.
  - Retired addresses keep working: `#hub/persona`, `#hub/voice`, `#hub/memory`
    land inside Agent, `#hub/budgets` on Hardware and `#hub/history` on Data,
    each rewritten in the URL bar so the next copy-paste carries the new one.
    Bare `#hub/agent` still means Runtime, which is where "Apply changes" is.
    The Data page's tab is in the hash for the first time (`#data/<tab>`), so it
    survives a refresh and can be linked into.
  - New pure `hub/hubRoute.ts` (+ vitest) resolves the whole Setup address;
    `HubView` is its only applier. The Agent sub-tab bar is `.hub-subtabs`, a
    distinct class from `.hub-tabs` so walkthrough and test selectors stay
    unambiguous now that tab bars nest.
- **The Vitals range filter is a dropdown.** Six chips (`Day … 5Y`) could not
  fit beside a title in a half-width panel header, so the group wrapped onto a
  second row and read as broken. It is a native `<select>`, which also brings
  arrow keys, type-ahead and the mobile wheel picker — where the old
  `role="tablist"` was decorative and arrows did nothing. With the width
  pressure gone the labels spell themselves out (`3 months`, `1 year`,
  `5 years`) and the range left the panel subtitle, which was repeating it.
  Operations' Live | Control stays a segmented control: two options, a view
  switch, not a filter.

### Fixed

- **Setup's Persona tab could take down the whole Setup view.** `PersonaPanel`
  maps `presets` and `format_choices` unguarded, so a `/api/hub/persona` that
  answers 200 with a partial body hit the error boundary — the same failure
  mode the skills payload was hardened against. Caught because the capture
  harness had no persona fixture and its `{ok: true}` default reproduced it
  exactly; the fixture now exists. The panel itself is still unguarded.

- **A connector's MCP token is no longer presented to its embedded UI.**
  `app_token()` resolved through `auth_env()`, which returns the first credential
  a manifest declares in any shape - including `mcp.token_env`. So a connector
  with both an `mcp:` server and `ui.embed: iframe` had the MCP server's bearer
  attached to every proxied request to `ui.url`: a token issued by one host handed
  to another. Setup → Connectors' own Connect-an-app form emits exactly that
  manifest when given an MCP url, a token and the sidebar-tile option. `auth_env`
  stays as-is (the Hub's credential field is keyed off it, and for an MCP
  connector the MCP token *is* the slot it owns); a new **`proxy_token_env`**
  answers the narrower question the app proxy actually asks - only
  `auth.token_env` or `ui.api.token_env`, the credentials a manifest states are
  the app's own. The SSO contract in `docs/CONNECTOR_SDK.md` §3 is unchanged.
  `tests/test_connector_egress_boundary.py`.
- **Connector egress no longer follows redirects.** Every outbound request made
  on a connector's behalf - both bridge app proxies, the agent's action proxy, the
  `ava-tools/1` discover facade, and the MCP client on both HTTP transports - used
  `requests`' default `allow_redirects=True`. An app answering `302 Location:
  http://127.0.0.1:8010/…` therefore had the bridge fetch loopback and return the
  body: an SSRF primitive with no guard, where `web_fetch` re-validates every hop
  against one. Two aggravations: the UI proxy forwards the app's own bearer, and
  `requests` strips `Authorization` only across *hosts*, so a same-host redirect
  resent the credential to an attacker-chosen path; and for MCP, 307/308 preserve
  the request body, so tool **arguments** would be resent to the redirect target.
  A 3xx is now refused explicitly, and - because the status check is unreachable
  for a notification - checked *before* the notification early-return, so a
  redirected `notifications/initialized` can no longer leave a session marked
  initialized against a server that never saw it. A static guard fails any future
  bridge egress call that omits the kwarg.
- **A misspelled consent tier no longer opens a tool up.** `_dynamic_access`
  tested `tier in _TIERS` and the fnmatch in one condition, so an unreadable tier
  made the pattern *not match*: `"*publish*": destrucive` fell through to the
  `write` fallback - grantable, always-allowable, and with no row in
  `load_errors()`. An author who writes a tier is asking for a gate, so a matched
  pattern with an unspellable tier now fails **closed** to `destructive`, and both
  `dynamic_access` values and static `access:` values are reported as
  `severity: error`. Previously only the block's *type* was checked, never its
  values. `tests/test_consent_tier_integrity.py`.
- **`confirm: [action_names]` is no longer silently dropped.** `_BLOCK_TYPES`
  declared `confirm` as `bool`, so `_validate` quarantined the list form before
  `_author_confirm` - which has always implemented it - could see it. An author
  who wrote `confirm: [publish_app]` got no confirmation prompt. It survived
  because `tests/test_approvals.py` exercises the list form through a mocked
  `connectors.load`, so the validator never ran on that path; the new coverage
  goes through the real loader.
- **Connector credentials are created 0600, not chmod'ed to it.**
  `set_env_secret` wrote at the ambient umask and fixed the mode afterwards, so
  under a permissive umask the file was briefly world-writable; `secrets/env/`
  itself was created at the umask too, leaving the directory that lists every
  connector credential 0755 on a umask-022 host. Both now carry their mode from
  creation, using the `opener=` idiom `audit.py` already uses.
- **`egress: {hosts: [...]}` rendered three defects in one line.** A bare public
  hostname became **port 80**, silently pointing a plaintext policy at an
  HTTPS-only service (now 443 for a public host, 80 kept for a private one). The
  blanket RFC1918 `allowed_ips` was attached to *every* host including public
  ones, pre-authorising that name to resolve into private space - which is the
  SSRF case `agent/policies/ava-knowledge.yaml` says the list exists to control;
  it is now emitted only for a host that is already private. And the endpoint
  carried **no `rules`**, meaning every method on every path, while
  `policy_inventory._scan` harvests wildcards only *from* rules - so the broadest
  grant a manifest could express was invisible to
  `ava_security_check.check_policy_wildcards`. A bare entry now states its own
  breadth as `* /**`, and new `egress.host_rules` narrows it.
  `tests/test_connector_policy_honesty.py`.
- **Service health no longer counts a 401 or a 404 as "up".** `dashboard._probe`
  returned `status_code < 500`, so an auth-gated probe URL, a deleted app, an
  over-quota 402 and a GET against a POST-only endpoint all painted a green pill
  and counted toward "Services up N/N". The default is now `2xx`; a manifest can
  say `service.expect: non5xx` (the old behaviour, now explicit) or pin an exact
  code, so the claim lives in the manifest rather than being assumed by the
  dashboard.
- **A remote server's self-reported consent tier is no longer trusted.**
  `_dynamic_access` consulted the `ava-tools/1` write-through cache, whose tiers
  come from the connector. The documented justification - a self-report "can only
  make a tool quieter, never extend its reach" - does not hold when quieter means
  `read`, and `read` means runs-silently-forever: a server returning
  `{"name": "wipe_everything", "access": "read"}` bought permanent silence on its
  own word after one discovery call. Self-reports are now honoured for a
  private/loopback connector (the case the field was designed for) and ignored for
  a remote one unless the owner sets `trust_declared_tiers: true`. Manifest
  `dynamic_access` patterns outrank both, as before.
- **A built-in connector can be turned off.** `POST /connectors/<cid>/enabled`
  refused anything without a user manifest, on the correct principle that Ava never
  rewrites the owner's shipped YAML - with the consequence that a shipped connector
  able to spend money had no UI off switch at all. Disabling a built-in now writes
  a two-line override stub into `$AVA_HOME/connectors/<cid>/`; `_merge_all` already
  resolves the user root over the built-in root by id, so no new mechanism was
  needed and the shipped file is still never touched. Refused with a 409 when both
  roots are the same directory (`AVA_HOME` unset, so it falls back to the code
  root), where the stub would overwrite the manifest it means to shadow.
- **OpenAPI-derived actions can take arguments.** `_actions_from_openapi` emitted
  no `input:`, so `render_tool` generated a tool declaring
  `additionalProperties: false` with no properties - the agent could pass nothing,
  and `GET /api/items/{id}` was requested with the literal `{id}` still in the
  path. It was also asymmetric: at or above `META_TOOLS_MIN` the meta-tool path
  builds schemas differently and arguments *did* work, so one manifest behaved
  differently either side of 16 actions. Path-level and operation-level
  `parameters` are now merged (per spec) with a JSON `requestBody`'s scalar
  properties, and a templated path segment is treated as required whatever the spec
  claims.

### Added

- **`apps.origin` - serve embedded connector UIs from their own browser origin.**
  Ava reverse-proxies a connector's UI under `/apps/<cid>/` on its *own* origin, and
  `AppFrame` sandboxes the iframe `allow-scripts allow-forms allow-same-origin` - a
  pairing that keeps Ava's origin. Ava serves no CSP and `/api/hub/*` has no CSRF or
  Origin check, so the session cookie was the only gate and a same-origin frame sends
  it: an embedded app's JavaScript could call Ava's API, including
  `POST /api/hub/approvals/<id>` to approve Ava's own consent prompts, and could read
  `parent.*`. Acceptable for a loopback app the owner wrote; not for a
  remotely-served one whose bundle can change without the owner touching Ava.
  **An `Origin` check cannot fix this** - the proxy makes the frame *genuinely*
  same-origin, so `Origin`, `Sec-Fetch-Site` and any CSRF token are byte-identical to
  the real SPA's. Setting `apps.origin` to a second hostname pointing at the same
  machine and port makes them two browser origins: `/apps/*` is served only on that
  host, everything else is refused there, and the shell hands the iframe a
  short-lived per-connector token via `GET /api/apps/<cid>/embed` because the apps
  origin deliberately has no session. No second listener and no systemd or Docker
  change - two names on one port are already two origins.
  **Unset by default, and that default is the unsafe one:** turning it on requires
  the owner to make a second name resolve to the box, so defaulting it on would break
  every existing install's app tiles. `/api/apps` now returns an `apps_origin`
  block carrying that warning so Setup can surface it.
  `ava_bridge/apps_origin.py`, `tests/test_apps_origin.py`.
- **`agent/mcp_server_connectors/` - the capability group that was minted and
  unused.** `install.sh` handed out a `connectors` token and `group_may()` enforced
  a `connectors` scope, but no server existed behind it, so the generated per-app
  tools and the device-event tool lived in `mcp_server_content` and ran on the
  **content** token. That is the group whose server runs `web_fetch` - the one place
  attacker-controlled text arrives - so a prompt-injected page could reach every
  connected app's action bridge and every device actuation. `SECURITY.md` §3's claim
  that the injection-bearing group's blast radius is bounded was not true of
  connectors. The fix gives the group its own server rather than narrowing the
  table: `install.sh` discovers it with no change, `device_events.mjs` moves into it,
  generated tools now land in `agent/mcp_server_connectors/apps/<cid>/`, and
  `content` keeps only what its remaining tools actually call. Narrowing the table
  first would have 403'd live tools instead. `tests/test_mcp_server_scopes.py` now
  derives each server's required scopes from the `/internal/...` routes its tools
  reference and fails **both** ways - a missing scope and a surplus one, the
  direction that breaks nothing and therefore survives.
- **A `sensitive` consent tier** - no side effects, but it discloses something.
  Ava's tiers conflated two independent axes: `read` meant "no side effects" and
  was implemented as "runs silently, forever". An author who wanted *"ask before
  you read my conversations"* had to label a read `write` (untrue about side
  effects) or `destructive` (untrue, and it trains the owner to tap through the one
  prompt that matters). `sensitive` is enforcement-identical to `write` - asks on
  first use, "Always allow" available - and simply says what it is asking about.
  `approvals.gate` already carries `access` on the pending record, so it reaches
  the prompt with no new plumbing. `tools_cache` can now represent every tier;
  it was short by two, so a device tool self-reporting `physical` was stored and
  enforced as the weaker `write`.

### Changed

- **A fork inherits no personality.** `agent/persona.txt.tmpl` used to hardcode
  one person's taste in how an assistant should talk - "in the spirit of Siri",
  "write like a person texting a friend", mirror the owner's profanity, "give
  real, unfiltered opinions" - with no config key to change any of it. The
  template now carries **only operational** directives (the tool-calling
  mandates, the never-fake-a-render rule, the deny-by-default network
  correction), and the assistant's voice comes from two new owner-settable keys,
  `persona.style` (free text, **empty by default**) and `persona.format`
  (`chat` | `markdown`). New **Setup → Persona** panel offers four starting
  points as editable text; what is saved is the text, never a preset id, so
  changing a preset upstream can never retroactively alter an existing install.
  `docs/PERSONA.md`, `tests/test_persona_neutral.py`.
- `persona.format` defaults to `chat` because Ava's chat surface renders replies
  as plain text - markdown headings and tables would appear literally. It is a
  renderer contract, not a preference, and lifting it is one setting away.

### Fixed

- **`/api/talk` ran the entire voice turn on the event loop.** ffmpeg decode (30s
  ceiling), the speaker embedding, CPU Whisper, the agent turn (`OC_TIMEOUT`, up
  to 600s) and an image pickup that polls for 120s were all synchronous calls in
  an `async def`, so one voice turn froze every SSE stream, the dashboard and the
  login gate for its whole duration. `/api/talk-text` had the same shape. Both now
  hand each step to `run_in_threadpool`, the idiom already used 9× in the same
  file. `tests/test_no_blocking_routes.py` reported green throughout because its
  curated blocklist named only two document helpers - the voice and turn seams are
  now in it, so the next one fails at review.
- **Chat's Code mode pointed users at a 404.** Flipping the composer toggle
  printed "open the classic UI at /legacy", and that panel POSTs to
  `/api/code/turn`, which no longer exists - only `/api/code/models` and
  `/api/code-turns` survived the move to the approval-gated Control Center. The
  message now names the Control Center, and the legacy panel says it is unwired
  instead of reporting "could not start".
- **Every published screenshot and tour video re-captured.** The tracked media
  still rendered an unannounced sibling project's name and a maintainer-local
  checkpoint name into the app sidebar, an approval banner and a model chip.
  `tests/test_no_owner_identity.py` scans text and cannot see a name baked into a
  PNG or MP4 - the gap its own docstring and `demo/README.md`'s accept checklist
  both call out - so a text-clean tree shipped 15 of 17 PNGs and all 6 videos
  carrying one. All re-shot from sanitized fixtures against the current build,
  at byte-identical published dimensions.
- The `ava-gpu` skill declared itself "fully uncensored" and was installed
  unconditionally, bypassing the `persona.adult` gate the owner sets (default
  off) which already governed the identical policy for conversation. Adult
  content is now one gate covering conversation and images alike; the skill
  defers to it instead of carrying its own always-on permission.
- A non-ASCII character in any compared secret raised `TypeError` from
  `hmac.compare_digest` and returned HTTP 500. On `POST /login` that locked the
  owner out of the only page that could change the password; on the `/internal`
  bearer check the token is caller-supplied, making it an unauthenticated way to
  force a server error. Added `security.constant_time_equals`.
- The documented Docker install baked `deploy/.env` and
  `deploy/ava-data/secrets/*` into the image: `.dockerignore` patterns were
  root-anchored, so a bare `.env` entry missed everything below the top level,
  and `install.sh` always passes `--build`.
- `agent/persona.txt.tmpl` and `agent/render_persona.py` are now in the
  self-editing approval tier. They were `auto`, so under `code.approval: policy`
  the agent could rewrite the operational mandates that constrain it and commit
  that itself.
- Three tests asserted against the gitignored `agent/policies/generated/`, so
  they failed on every fresh clone and in CI, which runs `pytest tests/` with no
  generation step. They now skip when the derived tree is absent.
- README and `deploy/README.md` claimed web search over Tor works on the Docker
  path (no profile provisions SearXNG or Tor), that `ava verify` "proves every
  advertised capability end-to-end" (it is a wiring and drift check), that Setup
  needs no terminal (agent provisioning and voice both do), and that the default
  image ships enough to enroll a voiceprint (`AVA_VOICE_DEPS=0`).

## [0.1.0] - 2026-07-28

First public release: Ava is cloneable. The install path documented in
README.md now works from a fresh checkout on someone else's hardware, verified
end to end on both the cpu and gpu profiles through to a real chat completion.

### Fixed - The documented install could not produce a chat message

An audit of the fork-and-self-host path found the application itself healthy and
**every defect on the install path**, in `deploy/`, where nothing was tested. The
common cause: the owner's own box was the only configuration anyone ran.

- **The container bound loopback inside its own netns.** `deploy/Dockerfile` left
  `AVA_HOST` at `127.0.0.1`, so the published port refused connections while the
  healthcheck - curling loopback *from inside* the container - reported `healthy`.
  A forker saw a green container and a dead browser tab. Now `AVA_HOST=0.0.0.0`
  in the image, with the container boundary doing the containing and compose
  publishing on `127.0.0.1:8096`.
- **CI could not have caught it.** `compose-smoke` booted with `docker run
  --network host`, which puts the container in the runner's own namespace, where
  a loopback bind *is* reachable. The job now boots through compose on the
  default bridge network and curls the published port from the runner, asserting
  both that compose reports healthy **and** that the external request succeeded.
  `tests/test_ci_covers_deploy.py` fails any reintroduction of `--network host`.
- **`deploy/local-serve.sh` was untracked** while a tracked script `exec`'d it and
  six tracked files referenced it. `tests/test_deploy_refs_tracked.py` now fails
  on any `deploy/*.sh` named in a tracked file but absent from `git ls-files`.
- **Every profile pointed at the GPU backend.** The `ava` service hardcoded
  `AVA_BACKEND_URL: http://vllm:8002/v1` for all five profiles while `vllm` only
  starts under gpu/full, so `--profile cpu` resolved a DNS name that did not
  exist. Each profile now ships a tracked `deploy/profiles/<name>.env` carrying
  `COMPOSE_PROFILES` plus its own backend trio, so `up`, `logs`, `down` and
  `pull` all interpolate identically.
- **The default model was a 30B checkpoint** needing ~35 GB of weights - the
  first command the README gives a new user, on hardware no consumer card has,
  retried forever by an uncapped restart policy. The shipped default is now
  `Qwen/Qwen2.5-7B-Instruct` in `deploy/default-model.env`, one line to change.

### Added - One table for every model's vLLM flags

`deploy/model-flags.conf` is the single source of truth for per-model
`--tool-parser`, `--reasoning-parser`, native context length and extra flags;
`deploy/resolve-model-flags.sh` reads it, both sourceable and runnable. Compose
and `local-serve.sh` previously carried separate, already-diverging tables, and
compose had no way to express `--reasoning-parser` at all. The `native_ctx`
column is what stops `--max-model-len` from being set above what the checkpoint
supports; it is left blank where the value is not certain, because a wrong entry
silently caps context instead of failing. `tests/test_model_flags_ssot.py` fails
if parser vocabulary reappears anywhere but the table and the docs.

### Security - Four ways in, closed

- **First-run takeover.** `/setup` is public and only checked "is setup needed",
  so on any non-loopback bind the first stranger to reach the port set the admin
  password. Ava now mints a one-time claim token at `$AVA_HOME/data/setup_claim`
  (0600) and prints it; setup is allowed from loopback or with a matching token.
  Not an outright refusal - a headless box must stay claimable.
- **The code agent could read secrets.** `access_policy` was consulted only on
  writes, so `read_file(".env")` returned `ANTHROPIC_API_KEY`, and
  `search("ANTHROPIC_API_KEY")` returned the key inline - `search` walks the tree
  itself and never touched the path resolver. The deny-list is enforced in
  `coder._safe()`, which every tool routes through, and restated in the two tools
  that do their own walk.
- **Connector Detect handed a pasted command the bridge's environment.**
  `hub_api._probe` built its spec without a `sandbox` key, so `mcp_client` took
  the uncontained branch with `{**os.environ}`. Unsandboxed stdio children now
  get `PATH`/`HOME`/`LANG`/`TMPDIR` plus their manifest's declared env, and the
  probe defaults to a Docker sandbox, failing closed when Docker is absent.
- **`/internal` scopes were documented, tested, and not enforced.** 24 of 25
  handlers passed no scope, so any valid group token reached every route -
  including `/internal/code-change` from the token held by the server that runs
  `web_fetch`, which is where prompt injection actually arrives. Enforcement moved
  into `auth.auth_gate`, deny-by-default for unclassified paths, so a route added
  later is covered without its author opting in.

### Fixed - Sessions, cookies, and who the client is

- `auth.cookie_secure` is now `true|false|auto` and resolved **per request**. It
  was `os.environ.get("AVA_COOKIE_SECURE", "1") != "0"` - a string compare where
  `false` evaluated true - and unconditional, so over plain HTTP the browser
  discarded the session cookie and the user bounced back to `/login` with no
  message, indistinguishable from a wrong password. That is the exact flow
  `docs/MOBILE.md` markets, and `qa/env_recipe.py` pinned the variable to `"0"`,
  so the suite structurally could not see it.
- One shared `auth.client_ip()` honours `X-Forwarded-For` only from
  `server.trusted_proxies`. The login throttle keyed on `request.client.host`, so
  behind any proxy the entire LAN shared one lockout bucket.

### Fixed - One typo in `ava.yaml` destroyed the file

`_load_config` swallowed the parse error and returned `{}`; `save_patch` then
merged the patch into nothing and wrote it back, non-atomically. A 13-line config
with one bad indent plus one Setup toggle became 2 lines. Parse errors now carry
their line number, surface on `/api/hub/system`, and **refuse the write** (409).
Writes go through one `os.replace` from a same-dir temp with a `.bak` of the
prior file. `settings.get_float` was added because `config.py` called bare
`float()` on `voice.threshold`, where a typo failed bridge boot with a raw
`ValueError`.

### Fixed - The setup wizard was skipped for everyone who used the CLI

`setup_completed()` returned true when `inference.backends` was non-empty, and the
shipped template ships one - so `ava setup` marked the box configured before the
owner had seen a single screen. It is now the completion flag alone, the wizard is
re-entrant and pre-fills from current config, and `ava setup` writes a minimal
`ava.yaml` instead of copying the annotated `config.example.yaml` template
(which `safe_dump` stripped of every comment on first save).
`POST /api/setup/save` validates before marking complete, rather than after.

### Fixed - Allocation across reboots, crashes, and concurrent callers

- Monotonic timestamps were persisted with no `boot_id`, so after a reboot the
  allocator could refuse every request for as long as the previous uptime, in
  silence. Records now carry `boot_id`; a foreign boot scrubs the clock fields
  and keeps the failure counts, because a reboot does not repair a broken model.
- `QUIESCED` was unreachable: the breaker tripped at `> limit` while the budget
  refused at `>= limit`, so the critical alert could never fire.
- Release intent is recorded **before** the driver call, so a crash mid-release
  cannot strand a model down; `_restore` re-checks under the model lock instead
  of around it; and reservations are credited against the observed pool drop
  (`need - max(0, free_at_grant - free_now)`) so a loading model is counted once.
- All three drivers now honour `wait_free`'s return value, which `base.py` had
  always specified and none implemented. Safe only because `ActionResult.acted`
  landed first, separating "it is stopped" from "the pool came back".

### Fixed - A broken manifest locked you out of the page that fixes manifests

A scalar `egress:` in any connector raised `AttributeError` from
`hub_api.list_connectors()` - a 500 on Setup → Connectors, the page containing the
manifest editor and the error list. Bad blocks are now quarantined **in memory
only** (never written back), reported through `load_errors()`, and the rest of the
connector keeps working. `manifest_version: 1` is declared, absent means 1, and a
newer version still loads with a warning: refusing to load on version mismatch is
what makes manifests non-portable.

### Fixed - 17 of 21 Setup panels swallowed their own errors

Every panel destructured `{ data }` off `useResource` and dropped `error`, so any
backend failure rendered Setup as tabs of permanent "Detecting hardware…" - each
one looking like it was still loading, forever, with the real message sitting
unread in a variable nobody named. Every call site now either binds `error` and
renders it, or hands the whole hook result to `<ResourceState>` /
`<ResourceError>`, which cannot drop the error because there is no field to
omit - both with a Try again button. `tests/test_hub_uniformity.py` fails a
regression; its one allow-listed exception is HubView's restart-banner fetch,
whose failure is deliberately silent.

### Fixed - Keyboard access, and a chat wedge

- `tabIndex` appeared **zero** times repo-wide. Sidebar conversation rows were a
  `<div onClick>` wrapping an invisible delete button - so the only
  keyboard-reachable control per row was the one control you could not see. Rows
  are now two sibling buttons in a wrapper, with `:focus-visible` /
  `:focus-within` companions to the `opacity: 0` rule, and "Forget" confirms.
- `useChat.send` awaited `ensureChat()` outside any try/catch, so a failure there
  skipped `setBusyBoth(false)` and the composer stayed locked until a page reload.

### Added - `ava` is a real command

`pyproject.toml` declares `[project.scripts] ava = "ava_cli:main"`, so
`pip install -e .` puts on PATH the command `README.md` has always told forkers to
run. Editable-only and deliberately so: `settings.CODE_ROOT` keeps resolving to
the checkout, which is what makes `config.example.yaml`, `frontend/dist`,
`agent/install.sh` and `connectors/_template` resolve at all. The package list is
an explicit allowlist, never `find:` - the repo root holds `agent/`, `config/`,
`connectors/` and `data/`, which must not become importable top-level packages.
`tests/test_cli_entrypoint.py` checks the list from both ends: nothing declared
that does not exist, and nothing imported by name that is not declared.

### Added - Model memory allocation (observe phase)
- **`ava_bridge/alloc/`** - fit-checked memory management for boxes that run Ava
  plus a second model. A box with a language model and an image pipeline
  oversubscribes its memory, so at most one can be resident; this layer answers
  *"can this model be brought up right now"* before anything tries, and reports
  what is actually holding memory. It gives `model_fit`'s cold-load predicate
  (`fits_now(..., assume_loaded=False)`) its first consumer.
- **Declared, never discovered.** Only models under `alloc.models` in `ava.yaml`
  are governed. Everything else holding memory is counted (so the planner never
  promises memory someone else has) and named, but never touched. **An absent
  `alloc:` block governs nothing** - an install that does not opt in behaves
  exactly as it did before.
- **Sizing is inherited, not repeated**: a declared model matching an
  `inference.backends.<id>` picks up `weight_gb`/`min_free_gb`/`tier` from its
  existing `fit:` block, and one matching a connector picks up that connector's
  unit and health probe.
- **Readiness means resident, not listening.** An engine binds its socket long
  before its weights load, and a service manager reports "active" the instant exec
  succeeds - so a model whose warm-up hit an out-of-memory error and carried on is
  invisible to any liveness check. Declare `readiness.require` and `ava doctor`
  reports `resident but NOT ready` instead of a green tick.
- **`ava doctor` → Allocation**: the pool with its source and learned baseline,
  memory held by undeclared processes, and per-model driver / residency (measured
  where possible) / cold-load verdict / config problems.
- **Portable by construction**: all memory reads go through the `hwinfo` HAL, so
  discrete-GPU, unified-memory, and CPU-only boxes each get the right source, and
  unreadable memory means *unknown*, which means never gate. A driver whose tooling
  is absent (no container runtime, no service manager) degrades to observe-only.
  New engine support is one adapter file - built-ins plus `$AVA_HOME/alloc_drivers/`.
- Decision recorded in `agent/docs/adr/0005-model-load-allocation.md`. This phase
  **observes only**; the lease broker that releases memory follows.

### Added - Allocation watchdog (nothing degrades silently)
- **`ava_bridge/alloc/watch.py`** - polls every declared model on an interval and
  raises a persistent alert for anything wrong, so a degraded model surfaces on its
  own instead of only when someone runs `ava doctor`. Uses the same
  `alerts.push_external` + `ttl = INTERVAL * 1.5` idiom as the architecture
  watchdog: an alert stays active while the condition persists and **self-clears
  once it is fixed**, with no acknowledge step.
- **The alert that matters**: `alloc_degraded_<model>` (critical) fires when a model
  is running but has no weights loaded. That state answers its own port and reports
  `active` to the service manager, so no liveness check detects it - the failure it
  was written for ran for six days on the development box. Also raised: declared but
  not installed, unable to start for a sustained period (with a grace window,
  because not fitting while something else holds the pool is the system working),
  driver misconfiguration, and undeclared processes holding memory while a declared
  model is blocked.
- **Alert-rule metrics** `alloc_degraded_count`, `alloc_unfit_count`,
  `alloc_unknown_hold_gb` in `dashboard.build_alert_metrics()`, with matching rules
  in `config/alerts.yaml`. Every metric is 0 until models are declared, so the rules
  stay dormant on an install that has not opted in.
- **Durable trail**: state changes go to the audit ledger as `alloc.<state>` with
  the previous state - transitions only, never one row per model per cycle.
- Started from the bridge alongside the other in-process schedulers; **no-op until
  models are declared**, so an install that has not opted in pays nothing.

### Added - Allocation leases (advisory)
- **`ava_bridge/alloc/policy.py`** - the planner, a **pure function**: given a
  request, the pool, and what each declared model holds, it returns an ordered plan.
  No I/O, no hardware, no clock, so the whole decision table is table-testable in
  milliseconds and a fork that disagrees with interactive-wins replaces one function.
- **Admission first**: if the request already fits, the plan is empty. Coordination
  schemes that pause a heavy model for *every* request pay a reload even when the box
  had room.
- **The correctness rule**: a live lease at the requester's own priority or better is
  **never preempted**. Everything else in the planner is an efficiency question; that
  one prevents corrupting work in flight. Lower-priority holders do yield, which is
  what declaring a priority is for.
- **`ava_bridge/alloc/ledger.py`** - cross-process ownership as a lock directory.
  Every quantity is *derived* from something the kernel guarantees: the refcount is
  how many lease locks are still held, and a dead holder is one whose lock can be
  taken. So a caller that is killed mid-work cannot leak a count, and crash recovery
  needs no timeout heuristic. Uses `flock` (not `lockf`, which is per-process and
  would give false exclusivity between threads).
- **`ava_bridge/alloc/broker.py`** - `alloc.lease("model", reason=...)`: a caller
  states a *need* and never names a victim. Reentrant per thread, reference-counted,
  with a cooldown so a burst costs one reload rather than one per request.
- **Advisory by default** (`alloc.lease.enforce: false`): the full decision is
  computed and appended to `logs/alloc.jsonl`, and **no driver is touched**. The log
  is the evidence for enabling enforcement later.
- **The funnel** - the lease is held across the whole GPU phase at the one submit
  every job must cross, so a pipeline added later inherits coordination by
  construction. Enforced two ways: a static guard fails any
  submit outside the transport (verified to catch a bypass), and a lease-less submit
  records `alloc.unfunneled` at runtime in case someone evades the scan. `alloc.roles`
  maps a subsystem to a declared model; unset means no lease is taken at all.
- `ava doctor` states **advisory vs enforcing** plainly, plus ledger health and any
  overdue holds.

### Added - Allocation enforcement (safe to switch on)
- **`ava_bridge/alloc/breaker.py`** - the safety limits that make enforcement
  something you can turn on. Two independent mechanisms, and the first matters more:
  - **A start that provably cannot fit is not attempted.** Measured through the real
    code path: with the pool permanently short, **0 start attempts over ~56 simulated
    hours**, no failures counted, no action budget spent, and the model stays
    retryable forever so it returns the moment room appears. A model waiting for
    memory is not a broken model. *Not attempting* is the cure; capping retries only
    limits the damage of attempting.
  - **Retries that do happen are bounded** - exponential backoff with jitter, giving
    up after 6 attempts or 30 minutes. Measured: a permanently failing start costs
    **6 attempts**, against the 7,997 an uncapped supervisor produced.
- **A failure record clears only after sustained readiness** (120s by default), never
  on one successful start - otherwise a model that crash-loops but looks healthy for a
  moment resets its own counter forever. Fed by the watchdog, which already polls.
- **Global action budget** across all models *and all processes* (state lives in the
  ledger, not in memory, so processes cannot each spend the full budget). Exceeding it
  puts the allocator in `QUIESCED`: it stops actuating and every lease becomes
  advisory. **Its failure mode is to become a no-op, never a loop.** Self-clears after
  a cool-off.
- **Two switches, not one.** `alloc.lease.enforce` allows action at all;
  `alloc.lease.evict` separately allows *releasing* a model, and defaults off. The
  halves carry opposite risk - waiting is at worst slower, releasing is at worst taking
  memory from work in progress - so the safe half can be enabled first.
- **`ava alloc status | plan <model> | restore | reset <model> | resume`** - the
  operator surface. `status` shows mode, pool, budget, held leases, and per-model
  breaker state; `plan` shows exactly what would be released and why. Giving up always
  names the command that clears it.
- New alerts: `alloc_giveup_<model>` and `alloc_quiesced`, both critical, both carrying
  the fix command.

### Added - Cross-app leases (one owner for the whole box)
- **`POST /lease` on the router** (plus heartbeat, release, status, restore) so a second
  application on the same box can hold a lease **without importing Ava**. That is how
  two apps stop fighting over one memory pool: exactly one component decides, and the
  other asks.
- **Token-guarded by prefix, unconditionally.** Unlike `/fit`, which only reads, these
  endpoints can stop and start models, so an unauthenticated caller could take Ava's
  brain down. Prefix matching (not the existing exact-match list) is required because
  the sub-paths carry a lease id.
- **Every handler runs the blocking work in a worker thread.** Acquiring may run a
  driver action taking tens of seconds, and awaiting that on the event loop would stall
  inference for every other caller.
- **A remote holder proves liveness with a heartbeat, not a file lock.** A local holder
  is reclaimed by the kernel when it dies; a caller reaching us over HTTP cannot be, so
  its lease carries a deadline and is reaped when renewals stop. Without that, a client
  killed mid-work would hold memory reserved forever. `snapshot()` reaps first, so a
  lapsed holder never appears live to the planner.
- **Unreachable is not the same as denied.** Denied is a decision to respect;
  unreachable means nobody decided, and a client must then fall back to whatever
  coordination it had before rather than running with none - which is the failure this
  layer exists to prevent. Safe because an allocator that cannot be reached is also not
  acting, so there is no second owner to contend with.

### Fixed - Residency was measured from the wrong place
- **A container runtime's and a service manager's memory figures under-report an
  inference engine by an order of magnitude**, because an engine's weights are device
  allocations and neither is charged for them. Measured on the development box for one
  language-model container: `docker stats` said **3.6 GiB**, per-process accelerator
  accounting said **43.7 GiB**. Acting on the smaller number made the planner project
  16 GB from releasing a 48.7 GiB model - it would have refused work that fits and
  over-released chasing a target it had already passed.
- **`ava_bridge/alloc/gpumem.py`** is now the residency oracle: per-process accelerator
  accounting, attributed to a container or unit through its process tree (an engine
  splits a launcher from the worker that owns the weights, so asking only about the pid
  a supervisor reports would miss nearly all of it). Both drivers prefer it and keep
  their own reading as the fallback for a CPU-side model or a box without the tooling.
  Unreadable stays distinct from holds-nothing - conflating them is how a model whose
  weights failed to load passes for healthy.
- This is the *pool* question's sibling, not a replacement: how much is free still comes
  from the `hwinfo` HAL, which knows a device-memory query returns nothing on unified
  memory. The convention guard enforces the split.

### Fixed - Allocation tests wrote into the real ledger
- Setting `AVA_HOME` at import looks like isolation but is not: `settings` freezes it on
  FIRST import, so in a shared pytest process where another module imported settings
  earlier the env var is a silent no-op. The watchdog tests call `note_ready()`, which
  writes to the ledger, and the fixture model names (`voice`, `ok`, `x`, `gone`, …) turned
  up in the live box's `run/alloc/breaker.json`. Harmless there - every entry had zero
  failures - but the same leak could have opened a breaker against a real model or
  written a learned memory baseline from faked readings.
- Fixed with the repo's path-seam pattern (patch the accessor, not the environment), and
  guarded: `tests/test_alloc_isolation.py` fails any allocation test that exercises a
  state-writing module without redirecting the path it writes to. Verified against a
  deliberate violation.
- `deploy/local-serve.sh` gains `AVA_SERVE_RESTART` (legacy alias `OMNI_RESTART`, still
  honoured through the `omni-serve.sh` shim). Docker's restart policy and the allocator
  are both supervisors, and only one can decide when there is room - an uncapped
  `unless-stopped` retries a start the allocator has declined, which is the mechanism
  behind the 7,997 restarts. The default is unchanged (right for a box with no
  allocator); `ava doctor` flags the combination when the container is declared.

### Changed - `POST /lease` can answer before the room exists
- **`"wait": false`** returns the verdict immediately with `state: "pending"`, runs the
  release on a worker thread, and the caller polls the new **`GET /lease/<id>`** until
  `ready`. Terminal states are `active`, `failed` (a release errored - the caller is
  uncoordinated, not blocked) and `gone` (the lease no longer exists, so nothing is
  acting on the caller's behalf and coordinating locally is safe again).
- A blocking acquire makes the client's socket timeout into a policy decision, taken by
  a number that knows nothing about what it is waiting for; now every request is a plan,
  a lock and a small write, so a timeout means the request did not land.
- **The default is still `wait: true`**, so a client written against the older contract
  is never told it may start before the memory is free.
- The verdict does not change while pending: `admit` comes from the plan, not from
  executing it, so the answer given first is the answer that stands.
- **One actor per model.** Answering immediately makes concurrent releases of the same
  model likely rather than rare, so each release now takes `models/<id>.lock` across the
  driver call. A requester that cannot get it waits for *the memory* instead of repeating
  the action - a duplicate `docker stop` landing after the peer has moved on to a restore
  is how a model comes back up mid-render.
- `ledger.update_lease()` merges instead of overwriting - the actuation thread and the
  heartbeat are now two writers on one record - and `write_lease` no longer restamps
  `started`, which had made a frequently-renewed lease permanently un-`overdue`.

### Fixed - Static guards were scanning zero files
- Every convention guard resolves its inputs with `git ls-files`, and the whole
  allocation layer was still untracked - so the ledger-isolation guard, and the guard
  forbidding the wrong memory oracle *inside* `ava_bridge/alloc/`, were both passing
  vacuously over an empty file list. Registering the paths turned up three genuine
  problems immediately.
- The decision log had its own leak: `_record()` writes under `logs/`, not the ledger
  dir, so redirecting the ledger never covered it - and whether a test polluted the real
  `logs/alloc.jsonl` depended on module import order. It got **91 fabricated decisions
  next to 2 real ones**, in the record an operator reads to decide whether to enable
  enforcement. Fixed with a `broker._log_path` seam, and guarded.
- A release now runs on a thread, so it can outlive the fixture that redirected the
  ledger - a static guard cannot see that, and `victim` duly appeared in the live box's
  `run/alloc/`. `broker.wait_for_actuations()` makes the redirection cover the whole
  action; the guard requires any test that starts a background release to call it.
- Two guard regexes were matching prose ("…to the audit ledger. Caught if…") and test
  *method names* (`..._no_acquire(self)`). All three guards verified by removing each
  seam in turn and confirming the failure.

### Documentation - the allocation layer
- **`docs/ALLOCATION.md`** - the forker-facing guide: why two models on one box is a
  problem that has nothing to do with either model working, how to declare one, how to
  read the decision log before letting Ava act, the two-switch rollout, what it will and
  will not do, writing a driver for your own engine, and the `MemFree`-vs-`MemAvailable`
  trap that makes monitors report 93% when a box is at 59%. Added to the published docs
  nav beside Hardware support, since that is the sensing layer this acts on.
- **Diagrams regenerated from the SSOT**: the system diagram gains an `alloc` node in the
  engines layer beside the HAL it reads; the network diagram gains the three edges that
  matter operationally - the studio asking `POST /lease`, and the allocator's stop/start
  and unload authority over the two engines. `ava-omni.service` is declared in `services`,
  which clears the drift warning it was raising.

### Changed - One supervisor for the resident model
- The LLM container is now created with `--restart no`, so the allocator is its only
  supervisor. Docker's `unless-stopped` would otherwise retry a start the allocator has
  declined, with no backoff - the mechanism behind the 7,997 restarts.
- **That needs a boot unit, and the pairing is the point.** `--restart no` also means
  nothing brings the container back after a reboot, and the allocator deliberately
  restores only what it released itself (`released_by_us` is a scope boundary, not just a
  safety net). So a `Type=oneshot` + `RemainAfterExit` unit runs `deploy/omni-serve.sh` at
  boot and then stops caring - it starts the container and does not supervise it, so it
  cannot fight the allocator when a render releases it. `StartLimitBurst` bounds systemd's
  own retries the way the breaker bounds the allocator's.
- Without both halves this is a regression rather than a fix: one supervisor, but a model
  that no longer survives a reboot.

### Changed - Deployment fossils cleaned up
- **`deploy/docker-compose.yml`'s vLLM service now sets its memory flags.** It passed
  only `--model`/`--port`, so a forker following compose got vLLM's ~90% default
  utilisation, CUDA graph capture, and an uncapped `restart: unless-stopped` - the exact
  combination that cost 7,997 restarts on the development box, and with the `full`
  profile it left nothing for anything started alongside it. Now
  `--gpu-memory-utilization` (default `0.90`, lowered where a second tenant shares
  the pool), `--enforce-eager` and `--max-model-len`, all
  env-overridable, and a capped `restart: on-failure:3` so a doomed start cannot loop.
- Router unit description no longer names two retired models.

### Added - Data page (the owner's data console)
- **Data view** (`ava_bridge/data_api.py`, `frontend/src/components/data/`): a
  built-in tab that inventories everything Ava stores under `$AVA_HOME` -
  Overview (per-store cards: path, format, size, counts, last write; secrets
  listed as counts only, never readable), Memory (the same governed browser as
  Setup → Memory), Chats (per-chat JSON/Markdown export + confirmed delete),
  Logs (newest-first tails of the audit ledger, performance log, and device
  streams), Maintenance (retention, `memory.db` integrity check + VACUUM,
  everything-archive export, one-folder backup story). `GET /api/data/stores`,
  `/chats`, `/chats/{cid}/export`, `/logs/{name}/tail`, `/maintenance` (+
  `integrity`/`vacuum` POSTs), `/export` - all behind the same cookie gate.
- **Everything-archive export** (`GET /api/data/export`) - memories, chats, the
  audit ledger, and `ava.yaml` as one `.zip`; secrets/keys are never included,
  media stays on disk (a full backup is a copy of `$AVA_HOME`).

### Added - Connector credentials (paste once, never re-prompt)
- **Save an app's token once, reuse it on every deploy.** The connect form now
  takes the actual **Access token / API key** (a password field), not just the
  name of an env var. Ava writes it `0600` to a new server-side store
  (`$AVA_HOME/secrets/env/<NAME>`, keyed by the manifest's `token_env`), so it
  survives restarts and every **redeploy** - you're never asked for it again.
  Forkers never touch `.env`: paste a value without naming a variable and Ava
  derives a stable one (`<CID>_TOKEN`). New `settings.env_secret` /
  `set_env_secret` / `clear_env_secret`; `POST /api/hub/connectors/{cid}/secret`
  sets/clears it; `list_connectors` reports `auth_env` / `auth_set` so a row reads
  **credential saved** or **needs a token**, with *Add / Update / Clear credential*
  in the ⋯ menu.
- **Invariant preserved (Ava-never-has-passwords).** The value is resolved only
  on the bridge, host-side, when an egress request is built - never placed in the
  global environment, never inherited by a subprocess (incl. the sandboxed
  agent), never written to the manifest or the generated tools. A real
  environment variable of the same name still wins. Every connector auth read
  (`connectors._auth_headers` / `_discover_headers` / `app_api` / `${VAR}`
  expansion, and `mcp_client` HTTP/SSE headers) routes through the one resolver;
  guarded by `tests/test_connector_secrets.py`.

### Added - Single sign-on for embedded apps (connect once, no re-login)
- **Ava presents a connected app's saved token to its embedded UI**, so an app
  with its own login never shows you its password screen again after you've
  connected it. The one credential now does double duty - the agent tools *and*
  the app's own web page. New `connectors.app_token(cid)` resolves the token
  (`token_env` → `settings.env_secret`); the same-origin app proxies
  (`/apps/<id>/…`) inject it as the bearer when the browser has none, and the
  saved token wins over a stale one left in the app's storage (no 401/login
  flash). Resolved only on the bridge - never handed to the browser or the
  sandboxed agent (Ava-never-has-passwords holds).
- **The app-author contract is a documented two-liner** (CONNECTOR_SDK.md §3
  *Single sign-on*): accept a static token (named by `token_env`) as a session,
  and skip your own login when embedded (a non-empty `/apps/<id>` mount). Apps
  can self-describe the token name in `/.well-known/ava.json`
  (`auth.token_env`) so the connect form prefills it. The template, `scaffold.py`
  README, and CONNECT_YOUR_APPS.md all cover it; the two bundled example apps
  were conformed to the contract.

### Changed
- **Chat deletion is audit-logged** - `DELETE /api/chats/{cid}` now writes a
  `chat_delete` event to the flight recorder, same as memory edits.
- **`MemoryPanel` extracted** to `frontend/src/components/hub/MemoryPanel.tsx`,
  shared by Setup → Memory and Data → Memory (one implementation).
- `memory_store.counts()` now reports `pinned`.
- **Setup UI redesigned onto one system** - all nine Setup tabs
  (`frontend/src/components/hub/`) share one visual grammar: typed identity
  tiles, tone-dotted status boards, overflow-safe action rows with a shared "⋯"
  overflow menu, and structured term/description legends. Connectors' **Deploy**
  is now state-aware (hidden once a connector's tools + policy are up to date -
  the row reads *deployed* and offers a quiet *Redeploy* in the ⋯ menu); the row
  action cluster wraps instead of overflowing the card. Hardware leads with the
  recommended tier; History types every audit kind with client-side category
  filters; Voice shows the speaker-gate state (closed / open / off); Budgets'
  meters always show usage (with an energy→$ readout) even before a cap is set.
- **Setup frontend refactored for uniformity** - extracted shared data/action
  hooks (`hub/hooks.ts`: `useResource`/`useAction`) and view primitives
  (`hub/ui/{Tile,Legend,Badge,StatRow,HubMessage}`), collapsed seven per-panel
  icon-tile classes and the scattered tone rules into one `.tile` + `--tone`
  system, and split the `HubView.tsx` monolith into `hub/panels/*.tsx` (one file
  per tab) behind a thin router (2883 → under 200 lines). Behaviour-preserving;
  enforced going forward by `tests/test_hub_uniformity.py`.

### Fixed
- **Icons sat off-centre in every tile / button / nav row** - the `<Icon>`
  wrapper's inline SVG inherited the text baseline's descender gap, so a
  flex-centred glyph rode high (and, once blockified, jammed to the left). `<Icon>`
  now tags its span `.ico` with `display:contents`, dropping the span from layout
  so the SVG centres directly on both axes inside its flex container - one rule
  that centres every icon app-wide (verified: vertical/horizontal offset 0).
- `.hub-note` / `.hub-restart` never sized a leading icon SVG (unbounded glyph);
  also fixes the Setup page's own restart banner.
- **Agent tab no longer crashes the whole Setup view** on a partial or errored
  `/api/hub/agent/skills` response - the skills loader normalises the payload and
  degrades to an empty list instead of throwing to the view error boundary.
- **Setup → System** optional-feature labels no longer run together (title/sub
  now stack), and Setup save-confirmations read green instead of the error red.

### Added - Setup Hub, MCP, governance & observability
- **Setup Hub** (`ava_bridge/hub_api.py`, `frontend/.../hub/`): a GUI onboarding &
  control portal - Overview, Models (hardware detect, pull-with-progress, bench),
  Agent (status/provision), Connectors (detect-then-connect an app, preview the
  generated tools + egress policy, deploy), Voice (browser-mic enrollment + gate
  test), Budgets, History (flight recorder), System. Everything writes `ava.yaml`.
- **Connect an app by detection** - one "where is your app?" field; Ava probes it
  (MCP over HTTP/stdio, or a discovery endpoint) and either finds the tools or
  asks for the REST actions. `POST /api/hub/connectors/probe`.
- **Wrap any MCP server in an egress policy** (`ava_bridge/mcp_client.py`): real
  MCP (JSON-RPC over Streamable HTTP or stdio) as a connector via an `mcp:`
  manifest block; the agent reaches only the two policed `__tools`/`__call`
  routes, allow-listed by the auto-generated policy.
- **Container-isolated MCP servers** - `mcp.sandbox: docker` runs a stdio server
  in a throwaway container (`--read-only`, tmpfs, cpu/mem/pid caps,
  no-new-privileges, no host mounts; `network: none` optional).
- **Human-in-the-loop approval gate** (`ava_bridge/approvals.py`) - mark an action
  `confirm: true` (or connector-level) and the agent's call blocks until you
  approve/deny in the Hub; `GET/POST /api/hub/approvals`.
- **Governed self-editing modes** - `code.approval: all | policy | none` (default
  `all`); secrets/`models/**`/`.git` always hard-denied.
- **Real learning cycles** - local-first self-analysis (router → Anthropic
  fallback) parks improvement proposals for approval, on a schedule + a "Run now"
  button; replaced the previously-dormant stubs.
- **Flight recorder** (`ava_bridge/audit.py`) - durable append-only audit ledger
  (turns, self-edits, tool calls) at `$AVA_HOME/logs/audit.jsonl`, surfaced on the
  History tab; survives restarts.
- **Cost & energy budgets** - `cost.budgets` (daily/monthly $ + daily kWh) with
  a 100% alert on each cap (plus an 80% early warning on daily cloud spend) and
  an idle-burn watch; editable in the Hub; honest "estimated" labeling when GPU
  power isn't measured.
- **Durable chain-of-thought** - reasoning steps persist with the chat message and
  replay on reload.
- **REST connector auth** - `auth: {token_env}` injects the app's bearer token
  server-side.
- **`ava models bench`** - same prompt across backends, TTFT/tokens-per-sec compare
  (CLI + Hub Compare button). **`ava verify`** - end-to-end claim check.
- **Tests**: connector-generator goldens (fixture-based), MCP client (http+stdio),
  budgets/audit, approvals, bench/CoT.

### Changed
- Docs reconciled with the code: "self-improvement" → governed **self-editing**;
  connector egress + tools documented as shipped (not "on the roadmap").
- Example/personal connectors removed from the tree; a fresh install lists only
  the infra connectors, so forkers connect their own app from a clean slate.

### Added - Publish-readiness (fork-portability pass)
- **Inference provider layer** (`ava_bridge/router_app.py`): the router is now an
  importable app factory with per-backend `engine` (vLLM/Ollama/llama.cpp/cloud)
  and `tools` flags, minimal engine adapters (vLLM reasoning kwargs, stream-usage
  injection, tool-capability routing), and **embeds in the bridge** at startup
  (`router_host.py`) - auto-detecting a standalone `ava-router` unit. Bare metal
  and Docker now ship the same bridge→router→engine product.
- **Router auth hardening**: `/v1/*` requires a bearer/`X-Ava-Router-Token` when
  bound off-loopback; loopback default stays open. Token in `secrets/router_token`.
- **First-run web wizard** (`/setup/wizard`): hardware+tier → backend → features
  → connectors, written to `ava.yaml` via `settings.save_patch`.
- **Connector capabilities** (generic, manifest-driven): `chat_pickup` (post-turn
  artifact quick-cards), `jobs` (GPU-attribution job polling), `model_hints`
  (loaded-model roles) - so app-specific chat/dashboard behavior is declared in a
  `connector.yaml`, not wired into core.
- **CI** (`.github/workflows/ci.yml`): ruff, pytest, frontend dist-drift, CPU-only
  smoke boot, gitleaks. New `ruff.toml`, `requirements-dev.txt`,
  `requirements-voice.txt`.
- **Security-surface tests** (42 → 108): router proxy/failover/auth, auth
  middleware + login throttle, SSRF guard (per-hop redirect revalidation),
  connector registry parsing.
- `bin/ava` is now tracked; `.env` is auto-loaded by the app itself (not just the
  run scripts); generic `AVA_SMTP_*` env (legacy `OUTLOOK_*` still accepted);
  timezone-truthful digest timestamps; frontend bundle code-split (React chunk).

### Changed - De-personalization
- First-party personal apps fully decoupled from tracked core (moved behind the
  connector manifest + overlay);
  owner-specific component/architecture docs moved to the gitignored `docs/dev/`;
  `CONTRIBUTING.md` rewritten for fork contributors; a voice-dep import guard so
  a fresh install boots without `requirements-voice.txt`.

### Added - Governance & security documentation
- `SECURITY.md` (trust boundaries, egress model, secret inventory, threat model),
  `CONTRIBUTING.md`, and an Architecture Decision Record set under
  `agent/docs/adr/`.

## [2026-07-06] - Productization: pluggable apps, agent runtime & Omni switchover

A large step toward "fork Ava, connect your own apps/hardware/models, run in
minutes." Four coherent work streams:

### Added - Connector / App SDK (data-driven app surface)
- **`ui:` manifest block + `/api/apps`**: the left rail/nav is now **data-driven**
  from the connector registry. A third-party app appears by dropping a
  `connector.yaml` folder into `$AVA_HOME/connectors/` - **no React/Python edits**.
- **Three embed tiers**: `native` (first-party React view), `iframe` (the app's own
  web UI, reverse-proxied **same-origin** under `/apps/<id>/` so it inherits the
  session cookie), `none` (generic action console).
- **Generic bridge infra**: `/apps/<id>/api/*` (token-injecting browser data-proxy),
  `/apps/<id>/*` (same-origin iframe proxy), and **dynamic tool discovery**
  (`actions.discover` → `__tools`/`__call`) so an app's whole MCP-style tool set
  bridges from a manifest.
- **`examples/hello-app/`** (a runnable third-party connector) and
  **`docs/CONNECTOR_SDK.md`**; `ava connector apps` CLI.

### Added - Fork-readiness & BYO
- **First-run web setup** (`/setup`): a fresh install prompts to create the admin
  password instead of a dead login wall.
- **Degraded chat fallback**: when no agent runtime is present, chat routes
  directly to any OpenAI-compatible endpoint (a working, tool-less assistant) -
  a fresh fork works day one instead of erroring.
- **UI dynamism**: header **model switcher** (renders the user's configured
  backends), **`/api/brand`** (re-brand name/tagline via `ava.yaml`, zero code
  edits), device-honest hardware labels (de-DGX'd), and `ava doctor` **model-tier
  recommendation** from detected memory.

### Added - Pluggable agent runtime
- **`ava_bridge/runtime/`**: an `AgentRuntime` interface with `NemoClawRuntime`
  (default) and `DirectRuntime` (fallback); `agent.py` is now a thin facade.
  NemoClaw (NVIDIA, Apache-2.0 - OpenClaw-in-OpenShell) is the first-class runtime.
- **`agent.required` gate** + `ava agent provision|status` + install.sh bootstrap +
  **`docs/AGENT_RUNTIME.md`**. Selection: configured runtime if available, else the
  Direct floor; `required: true` makes the full runtime a hard requirement.

### Changed
- **Config is now driven by `ava.yaml`** for the running bridge (`config.py` layers
  env → `ava.yaml` → defaults via `settings`); `serve.py` binds host/port from it.
- **First-party apps fully migrated onto the generic connector proxy** - bespoke
  per-app routes removed; egress is auto-generated per connector
  (`agent/policies/generated/<app>.yaml`). The drift-check now
  recognizes generated connector policies.
- **De-personalized** for forks: `.env.example` sanitized (no personal paths),
  `config.PROJECTS` is dynamic (only apps whose checkout exists), report email is
  config-driven, and dashboard/perf fallbacks are core-only.

### Fixed - Omni agent switchover
- **Ava's agent now runs on open-model 30B** (Super-120B fully retired). The sandbox
  agent's own inference config was repointed from Super to Omni.
- **`vllm-open` served at 65536 context** (was 32768) - the agent's ~29k-token
  system context now fits; `deploy/omni-serve.sh` default bumped.

### Security
- **`vllm-open` bound to `127.0.0.1` only** (was `0.0.0.0`) - inference is no longer
  exposed on external interfaces; the sandbox reaches it via the host-side guard
  proxy. `ava_security_check.py` passes.

## [2026-07-03] - Central model hub

### Changed
- **All model weights consolidated into a single machine-wide hub** (see
  `paths.models` / `AVA_MODELS_DIR`), catalogued by a registry file. Duplicated
  weights removed (verified byte-identical before deletion).
  - vLLM loads via `HF_HOME` inside the hub.
  - Voice models (Piper, faster-whisper, ECAPA) live under the hub; only the
    biometric `voiceprint.npy` stays app-local.
  - The Ollama store is consolidated under the hub's caches.

### Added
- **Model Hub** node in the system diagram (engines → "load weights" → the
  hub), regenerated from the manifest.

## [2026-06-28] - Self-maintaining architecture pipeline

### Added
- **SSOT pipeline:** `agent/docs/architecture.yaml` as the single source of truth;
  `agent/docs/arch.py` generates the system & network diagrams and a
  services-and-ports table, and drift-checks the manifest against the running
  system.
- Five `architecture` MCP tools (`get_architecture`, `describe_component`,
  `check_drift`, `sync_diagrams`, `update_architecture`) so Ava can read and update
  her own architecture, gated by the `ava-knowledge` policy.
- Automation: `ava-arch-sync.path`/`.service` watcher + git pre-commit drift gate.
- Comprehensive docs: `README.md`, `agent/docs/README.md` (deployment-specific
  component notes live outside the public repo).

### Security
- App password gate on `:8445` (HMAC-signed session cookie, per-IP login throttle).

## [2026-06-26] - Voice and phone bridge

### Added
- Voice loop (`voice_ava.py`): Whisper STT → local vLLM → Piper TTS, with an
  ECAPA-TDNN speaker gate (your-voice-only).
- Phone voice/chat bridge (`phone_bridge.py`) served over Tailscale at `:8445`.
- Native MCP `get_weather` tool (Open-Meteo) under the narrow `ava-weather` policy.
- Routed chat through the OpenClaw agent (`main`) instead of raw vLLM.
