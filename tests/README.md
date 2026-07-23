# tests/ — unit & module test suite

Fast, isolated tests for individual `ava_bridge` modules. This suite proves the
**parts**; the whole application (the real `phone_bridge:app`, driven as a new
user and a continuing user) is proven by the separate [`qa/`](../qa/README.md)
suite. Keep them separate: `qa/` pins `AVA_HOME` and a hermetic environment at
import time and must own its process, so it is never collected here.

## Run

```bash
.venv/bin/python -m pytest tests/ -q       # the whole suite (~3s, no GPU/network)
.venv/bin/python -m pytest tests/test_router.py -q   # one file
python tests/test_perf_store.py            # most files also run standalone
```

Style: stdlib `unittest` classes, pytest as the runner. No `conftest.py`, no
shared fixtures — every file is self-contained. Dev deps: `requirements-dev.txt`
(pytest, ruff). Lint: `ruff check .` (config in `ruff.toml`).

## What each file covers

| File | Covers |
|---|---|
| `test_approvals.py` | JIT consent semantics (`approvals.needs_confirm`): reads silent, ungranted writes ask, author-confirm always asks |
| `test_auth.py` | `auth.auth_gate` middleware: cookie HMAC, public vs gated paths, login throttle, internal-token gate on `/internal/*` (401 before validation — never a schema-leaking 422) |
| `test_backends_manager.py` | Hub multi-backend "brain" manager: add/list/test/set-brain/delete + cloud-key wiring |
| `test_bench_cot.py` | `bench` model compare + `chat_store._trim_steps` durable chain-of-thought |
| `test_budgets_audit.py` | Cost budgets, idle-burn metric, audit ledger (0600 perms, newest-first, kind filter) |
| `test_connector_generators.py` | Connector codegen: determinism, tool↔policy lockstep, MCP policy = exactly `__tools`/`__call` |
| `test_connectors.py` | Connector registry: manifest parsing, `${VAR}` expansion, capability accessors |
| `test_diagram_sync.py` | Rendered `.svg` `d2sum:` stamp matches its `.d2` source (SSOT drift guard) |
| `test_fastapi_compat.py` | Canary for the fastapi/starlette prefixed-router 404 regression |
| `test_grants.py` | Consent access tiers, grants store, tier-aware gate |
| `test_hardware_models.py` | Hardware monitor model inventory: engine worker processes merge into one row; model identity read from cmdline/config/API, never assumed from runtime kind |
| `test_hub_uniformity.py` | Static guard on the Setup (Hub) **frontend** (`frontend/src/components/hub/`): shared `Badge`/`StatRow` aren't re-hand-rolled, no resurrected per-panel classes (icon tiles, action clusters, tone rules), one `.tone-*`/`--tone` system — no build/browser needed |
| `test_hwinfo.py` | Hardware-abstraction decisions across Apple / CPU-only / no-psutil |
| `test_mac_setup.py` | Non-CUDA onboarding: a high-RAM Mac must not get the vLLM default |
| `test_mcp_client.py` | MCP client: real stdio subprocess session, HTTP+SSE stub, Streamable-HTTP, connector integration |
| `test_memory.py` | Long-term memory store/recall/distiller + hub surface (see docs/MEMORY.md) |
| `test_model_fit.py` | Model-fit engine: tier/workload ordering, memory shedding |
| `test_perf_sources.py` | Live perf sources (`perf_mgmt.sources` + `app_perf`): connectors appear with no restart, history survives disconnect/reconnect, corrupt-ledger tolerance, bounded action-log writer |
| `test_perf_store.py` | Perf cold store: no data loss, idempotent rollups, retention, tok/s clamp, histogram percentiles, hot/cold stitched readers |
| `test_probe_wellknown.py` | Hub probe `/.well-known/ava.json` self-description prefill |
| `test_remote_runtime.py` | `runtime.remote` ↔ agent-container contract (auth header, live-CoT proxy) |
| `test_router_host.py` | Embedded router lifecycle: disabled / external / embedded |
| `test_router.py` | Inference router: model rewrite, failover ordering, engine adapters, control auth — no network, no GPU |
| `test_scaffold.py` | `ava app new` scaffolds a conformant ava-tools/1 surface |
| `test_security.py` | Scoped internal tokens + config/policy managers |
| `test_setup_wizard.py` | `settings.save_patch` + wizard completion gate |
| `test_tooling_note.py` | Undeployed-tools awareness in turns ("deploy first", never invent results) |
| `test_web_fetch.py` | SSRF guard: IP/URL validation, redirect-hop revalidation |

## Isolation patterns (use these in new tests)

- **Unconditional `AVA_HOME`** — at the very top of the file, *before* any
  `ava_bridge` import: `os.environ["AVA_HOME"] = tempfile.mkdtemp(...)`.
  Never `setdefault`: on a box that already exports `AVA_HOME` it's a silent
  no-op and your test writes into the real ledger (the commit 8d67dc3 bug).
  Note `settings` freezes `AVA_HOME` at first import — if another test file
  imported it first in the same pytest process, the env var alone is not
  enough; also patch the module's path seam (see the next two patterns).
- **Global rebinding** — save/restore module globals looked up at call time
  (`test_perf_store.py`: `ps.ROLLUP_DIR`, `perf_mgmt.SOURCES_OVERRIDE`,
  `perf_mgmt.LEDGER_PATH`, `app_perf.APPS_DIR`) in `setUp`/`tearDown`.
- **Path-seam patching** — when a module caches a path, patch its accessor
  (`test_memory.py` patches `memory_store.db_path`) rather than relying on env.
- **Injectable transports** — never hit the network: `httpx.MockTransport`
  through the seams built for it (`router_app.create_app(transport=…)`,
  `web._make_client`), or a threaded stdlib `http.server` stub.
- **Minimal apps over the real app** — auth/middleware tests build a small
  FastAPI app with just the piece under test; importing `phone_bridge` is
  heavy and belongs to `qa/`.

## Related checks elsewhere

- `qa/run.sh` — the whole-app suite (in-process app, live subprocess, fixture
  contracts, CLI, browser E2E). See [qa/README.md](../qa/README.md).
- `ava verify` — end-to-end claim checker (generator drift, governance wiring,
  learning/memory wiring, service probes); run by CI's smoke job.
- `ava doctor` — environment/health check for a live install.
- CI (`.github/workflows/ci.yml`): lint → this suite → frontend-dist drift →
  CPU-only boot smoke + `ava verify` → secrets scan → docs build.
