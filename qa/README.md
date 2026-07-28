# qa/ — whole-app QA suite

`tests/` proves the modules; **this folder proves the application** — the real
`phone_bridge:app`, driven the way a new user and a continuing user drive it.
Hermetic by default: no GPU, no models, no API keys, no outbound network. The
"model" is a fake OpenAI-compatible stub; the GPU service and the user's connected app
are fakes too (`qa/fakes/`).

## Run it

```bash
qa/run.sh              # everything (backend tiers + frontend E2E)
qa/run.sh --backend    # pytest tiers only
qa/run.sh --e2e        # Playwright vs a real bridge subprocess only
.venv/bin/python -m pytest qa/test_04_connectors_lifecycle.py -q   # one file
```

**Never mix into `pytest tests/`.** `qa/conftest.py` pins AVA_HOME and the
hermetic env at import time (settings freezes AVA_HOME on first import), so this
suite owns its process. `pytest tests/` remains the fast unit suite.

## Layout

| Tier | Files | What it proves |
|---|---|---|
| fakes | `fakes/fake_llm.py`, `fakes/fake_gpusvc.py`, `fakes/fake_app.py` | scriptable model / the GPU service / user-app stand-ins |
| 1 — in-process app | `test_00`–`test_11` | the real FastAPI app under TestClient: new-user journey, generated auth sweep over every route, API contracts the SPA reads, chat+turns, connector lifecycle (incl. approvals + history-across-reconnect), device ingest, media jobs, hub settings, dashboards+SSE, data lifecycle/export, security posture, graceful degradation |
| 2 — live process | `test_20`–`test_22` | `serve.py` as a subprocess: cold boot, two instances coexisting, restart persistence (chats/memory/settings/session cookie), real-socket SSE + static/PWA serving |
| contracts | `test_30_fixture_contract.py` | demo/ fixtures never drift from the real API shapes |
| CLI | `test_40_cli.py` | `ava setup/doctor/version/connector/device` in isolated homes |
| 3 — browser E2E | `e2e/*.spec.ts` via `e2e/run_e2e.py` | real Chromium against the real bridge: first-run setup, login→chat→reply, Vitals/Ops/Data rendering live data, connect-an-app appearing in Vitals |

Deliberately not exercised live (their *degradation* paths are tested instead):
real GPU inference, real the GPU service renders, Anthropic-billed code-agent calls,
NemoClaw provisioning, voice model downloads.

## Conventions

- `qa/env_recipe.py` is the single source of the hermetic env (used by
  conftest, the subprocess launcher, and the CLI tests).
- `qa/_shims/faster_whisper.py` blocks the optional voice stack so boots are
  fast and `/api/talk` deterministically reports "voice not installed".
- Test files are numbered because some tell one ordered story (the new-user
  journey, the connector lifecycle).
- Tier 3 needs `frontend/dist` built and `demo/node_modules` installed;
  it self-skips with a note when either is missing.
