# Contributing to Ava

Thanks for helping build Ava - a self-hosted personal AI operating layer.
This guide covers the development workflow for a fresh clone or fork.

Ava is maintained by **Joshua Thompson** ([@JayMTea](https://github.com/JayMTea)).
Open an [issue](https://github.com/JayMTea/Ava/issues) to ask anything, propose
something, or just say what you are building - that is the front door, and it is a
better one than email because the answer stays where the next person can find it.
Security reports go privately instead, per [SECURITY.md](SECURITY.md).
For anything longer-form or work-related, LinkedIn: <https://www.linkedin.com/in/joshua-thompson-b89913105>.

**The most valuable contribution right now is a hardware report.** Ava claims four
first-class platform families and only some are verified on real silicon; the rest are
labelled `ci-simulated` in [deploy/platforms.conf](deploy/platforms.conf) precisely
because nobody has run them. If you have an AMD Strix Halo, a discrete Radeon, an
Apple Silicon Mac or a plain x86 box, `python3 tools/ondevice_check.py --record
--json` produces the fixture and report that promote a row from claimed to verified - 
or a concrete defect list, which is just as useful.

## 1. Local setup

```bash
git clone <your fork> && cd Ava
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cd frontend && npm ci && npm run build && cd ..

./bin/ava setup      # dirs, secrets, admin password, ava.yaml
./bin/ava doctor     # hardware, config, inference route, services
./bin/ava up         # web app on http://localhost:8096
```

**`doctor` exits 2 until something can answer a prompt, and that is expected on a
fresh clone** - you have no engine yet, so the inference row is red by design.
The app still starts, and every surface that does not need a model works. When you
do want one, the shortest path on any machine (no GPU required) is the Ollama
profile:

```bash
cd deploy && cp profiles/cpu.env .env && docker compose up -d
```

You do not need a model at all to work on the SPA, the Setup hub, the docs, or
anything in `tests/` - the suite is hermetic and needs neither a GPU nor a
running bridge.

Optional extras: `pip install -r requirements-voice.txt` enables STT + the
voice gate (the app runs voice-less without it), and
`pip install -r requirements-docs.txt` builds the docs site (`docs-site/`).

### Previewing the docs site

```bash
python docs-site/sync.py && mkdocs build --strict -f docs-site/mkdocs.yml
python docs-site/preview.py          # -> http://127.0.0.1:8099/Ava/
```

`preview.py` serves the built `docs-site/site/` - the exact artifact Pages
publishes - at the `/Ava/` prefix from `site_url`, with HTTP Range support so
the walkthrough videos can be scrubbed. `mkdocs serve -f docs-site/mkdocs.yml`
is fine for prose (it live-reloads, and note it also mounts at `/Ava/`, not
`/`), but it answers a `Range:` request with the whole file, so video seeking
does not work there and a media check on it is not a real one.

## 2. Tests & lint

```bash
python -m pytest tests/     # pure-logic tests - no GPU or network needed
ruff check .                # Python lint (config in ruff.toml)

cd frontend
npm run lint                # SPA lint (Biome - config in frontend/biome.json)
npm test                    # SPA unit tests (Vitest)
npm run typecheck           # tsc, no emit
```

CI runs **14 jobs** on every PR: `ruff`, a DCO sign-off check, the frontend
checks (Biome lint + Vitest), `shellcheck`, the `tests/` tier, the `qa/` backend
tier, **`qa-e2e` browser specs**, a frontend dist-drift check, a CPU-only smoke
boot plus `ava verify`, three compose checks (config, install, smoke), a secrets
scan, and a strict docs build.

Two of those bite people who did not know they existed:

- **`qa-e2e`** drives the built SPA in headless Chromium, and it treats a *skip*
  as a failure - so it cannot be dodged by not having a browser. If you touch
  `frontend/src/`, run it before pushing:
  ```bash
  cd qa/e2e && npm install && cd ../..
  bash qa/run.sh --e2e
  ```
- **`frontend-dist-drift`** rebuilds the bundle and byte-compares it with the one
  you committed. Use the Node version in `frontend/.nvmrc` (`nvm use` in
  `frontend/`); a different major produces a different bundle, and the diff will
  look like a change you did not make. `npm run build` refuses to run on the
  wrong major rather than letting you find out from CI.

Why Biome and not ESLint: `typescript-eslint` refuses to load against TypeScript
7 (`typescript-eslint does not support TS 7.0`), and the SPA is on 7.x. Biome
ships its own parser, so it is independent of the TypeScript version.

## 3. Frontend changes

`frontend/dist/` is **deliberately tracked** - the FastAPI bridge serves the
prebuilt bundle so a fork needs no Node at runtime. If you touch
`frontend/src/`, rebuild and commit the regenerated `dist/` in the same
commit (`cd frontend && npm run build`). CI fails the PR if `dist/` drifts
from `src/`.

If two branches both touch the SPA you will get a merge conflict in `dist/`,
because the bundle filename is content-hashed and `dist/sw.js` is one long line.
**Never hand-merge it.** Take either side, rebuild, and commit the result:

```bash
git checkout --ours frontend/dist   # either side works; it is regenerated
cd frontend && npm run build && cd ..
git add frontend/dist
```

### Working on the SPA without rebuilding every time

You do not need to run `npm run build` per iteration. Vite's dev server proxies
the API to a running bridge, so hot reload works against real data:

```bash
./bin/ava up                     # terminal 1: the bridge on :8096
cd frontend && npm run dev       # terminal 2: the SPA with hot reload
```

Point it at a bridge on another host or port with `AVA_BRIDGE`:

```bash
AVA_BRIDGE=http://192.168.1.50:8096 npm run dev
```

Same-origin cookie auth keeps working through the proxy, so you log in once in
the dev server and stay logged in. Commit the built `dist/` when you are done -
the dev server does not produce it.

## 4. Conventions

Use Conventional Commits:

```
feat(bridge): add <thing>
fix(router): <thing>
docs(connectors): <thing>
chore: <housekeeping>
```

All contributions require a **DCO sign-off** - commit with `git commit -s`
(adds `Signed-off-by: Your Name <you@example.com>`). What you are certifying is
in [`DCO`](DCO); CI checks it on every commit in your PR.

### Conventions a test will fail you on

These are enforced by static guards, not review, so they are worth knowing before
you write the code. The full write-up is [`CLAUDE.md`](CLAUDE.md) - it is
addressed to AI coding agents but it is the accurate statement of this repo's
working conventions, and humans should read it too.

| Rule | Guard |
| --- | --- |
| Every optional capability is one entry in `ava_bridge/features.py` and is gated with `features.preflight(...)`, never a hand-rolled `settings.get_bool("features.…")` | `tests/test_feature_convention.py` |
| Anything representing a connected app carries its accent/icon via `appAccent()` / `appIcon()` | `tests/test_hub_uniformity.py` |
| Setup panels build from `hub/hooks.ts` + `hub/ui/`, with one tone system | `tests/test_hub_uniformity.py` |
| An `async def` route does no blocking work - hand it to `run_in_threadpool` | `tests/test_no_blocking_routes.py` |
| A new route is added to the frozen route table in the same commit | `tests/test_route_table_stable.py` |
| Nothing personal or machine-specific in tracked files, prose included | `tests/test_no_owner_identity.py` |
| The shipped persona template stays operational-only - no personality | `tests/test_persona_neutral.py` |

Significant or hard-to-reverse decisions get an **ADR** under
[`agent/docs/adr/`](agent/docs/adr/) (copy `0000-template.md`).

Cutting a release (maintainers): see [docs/RELEASING.md](docs/RELEASING.md) -
SemVer, signed tags, and the automated signed-image pipeline.

## 5. Glossary

Several of these words are overloaded, and a couple used to rhyme with each other
while meaning different things. Read this once and the module names stop looking
arbitrary.

| Term | What it means here |
| --- | --- |
| **bridge** | The FastAPI app: `phone_bridge.py` + `ava_bridge/`. Serves the SPA, authenticates, and is the only thing a browser talks to. |
| **router** | The OpenAI-compatible inference proxy in front of whichever engine you run (`ava_bridge/router_app.py`, hosted by `router_host.py`). Logs perf, does not decide anything. |
| **engine** | The thing that actually runs the weights: vLLM, Ollama, llama.cpp, or a cloud endpoint. |
| **backend** | One configured engine + model + URL, from `inference.backends` in `ava.yaml`. |
| **brain** | The backend a chat turn thinks with. With the agent runtime active that is the sandbox model; the picker then steers only the fallback. |
| **agent runtime** | The sandbox that gives Ava tools, skills and memory - NemoClaw by default, `direct` (tool-less) when absent. `ava_bridge/runtime/`. |
| **connector** | A manifest (`connectors/<id>/connector.yaml`) wiring an external app in: health, metrics, agent tools, generated egress policy. The extension model. |
| **app** | A connector's user-facing identity - what appears under "Apps" in the sidebar. |
| **action** | One callable operation a connector declares. Becomes an agent tool. |
| **tool** | Anything the agent can call, whether from a connector action, an MCP server, or built in (`get_weather`, `run_gpu_job`). |
| **skill** | A `SKILL.md` telling the model *how and when* to use tools. Instructions, not capability - `agent/skills/`. |
| **the dashboard** | Vitals + Operations. (Previously also called the "Command Center"; that name was retired for rhyming with the next row.) |
| **Control Center** | The approval queue *inside* Operations → Control, where parked code changes and learning proposals wait for you. |
| **turn** | One request/response cycle with the agent, tracked in `state.turns` with a live chain-of-thought. |
| **fit memory** | The memory pool a model is sized against - VRAM on a discrete GPU, system RAM on a unified box. `ava_bridge/hwinfo.fit_memory()`. |

## 6. Ground rules (keep Ava fork-portable)

- **Config over hardcode** - no literal paths, ports, hostnames, or model ids
  in code; everything resolves via `ava_bridge/settings.py` (env →
  `$AVA_HOME/ava.yaml` → default).
- **Connectors, not `if myapp:`** - app integrations go through the connector
  manifest ([docs/CONNECTOR_SDK.md](docs/CONNECTOR_SDK.md)), never bespoke
  wiring in core.
- **Nothing personal in the repo** - secrets are `0600` and gitignored under
  `$AVA_HOME/data/` (login password, session key, internal token) and
  `$AVA_HOME/secrets/` (router token, connector credentials) - see
  [SECURITY.md §4](SECURITY.md#4-secret-inventory); new network access is a
  narrow egress policy, not a broad allow.

## 7. Architecture SSOT (maintainer automation - optional)

Deployment topology lives in a **gitignored, deployment-specific** manifest
(`agent/docs/architecture.yaml`) from which diagrams and drift checks are
generated by `agent/docs/arch.py`. On a fresh clone that manifest doesn't
exist and every `arch.py` subcommand except `update` is a clean no-op skip -
and `update`, which rewrites the manifest, is maintainer-only. You don't need
any of it to contribute. See [agent/docs/README.md](agent/docs/README.md).

## 8. Before you push

- [ ] `python -m pytest tests/` and `ruff check .` pass
- [ ] `bash qa/run.sh --backend` passes (the tier that exercises the real ASGI app)
- [ ] `shellcheck -S warning deploy/*.sh bin/ava qa/run.sh run.sh run_bridge.sh` passes
- [ ] Docs build: `python docs-site/sync.py && mkdocs build --strict -f docs-site/mkdocs.yml`
- [ ] SPA touched? `cd frontend && npm run lint && npm test` pass
- [ ] SPA touched? `bash qa/run.sh --e2e` passes (a skip is a failure in CI)
- [ ] Frontend rebuilt + `dist/` committed if `src/` changed, on the Node in `frontend/.nvmrc`
- [ ] No secrets or personal data in the diff (CI runs a secrets scan)
- [ ] New network access expressed as a narrow policy/connector egress
- [ ] `CHANGELOG.md` updated for user-facing changes
- [ ] Commits signed off (`git commit -s`)
