# tests/ — unit & module test suite

Fast, isolated tests for individual `ava_bridge` modules. This suite proves the
**parts**; the whole application (the real `phone_bridge:app`, driven as a new
user and a continuing user) is proven by the separate [`qa/`](../qa/README.md)
suite. Keep them separate: `qa/` pins `AVA_HOME` and a hermetic environment at
import time and must own its process, so it is never collected here.

## Run

```bash
.venv/bin/python -m pytest tests/ -q       # the whole suite (~20s, no GPU/network)
.venv/bin/python -m pytest tests/test_router.py -q   # one file
python tests/test_perf_store.py            # most files also run standalone
```

Style: stdlib `unittest` classes, pytest as the runner. No `conftest.py`, no
shared fixtures — every file is self-contained. Dev deps: `requirements-dev.txt`
(pytest, ruff). Lint: `ruff check .` (config in `ruff.toml`).

## What each file covers

| File | Covers |
|---|---|
| `test_alloc.py` | Allocation: absent config governs nothing, unknown memory never gates, a driver whose tooling is missing degrades to observe-only, and port-open is never "ready" |
| `test_alloc_api.py` | Lease HTTP surface: every route token-guarded (it can stop models), a remote holder reaped when it stops renewing, a local holder never reaped on a deadline, and an allocator error still answers "proceed" |
| `test_alloc_breaker.py` | Retry storm bounded (a permanently failing start costs ≤6 attempts, not thousands), a start that cannot fit is deferred not failed, one brief success never clears a crash-loop's record, and the action budget makes the allocator a no-op rather than a loop |
| `test_alloc_gpumem.py` | Residency oracle: per-process accelerator accounting beats a cgroup figure (which under-reports an engine ~12x), a process tree is summed not just its root, and unreadable stays distinct from holds-nothing |
| `test_alloc_funnel.py` | Convention guard: GPU work goes through `gpu_service.gpu_lease` (the one `POST /prompt`), so a pipeline added later inherits coordination instead of silently opting out; plus a check that the allowed module really holds the lease |
| `test_alloc_isolation.py` | Convention guard: an allocation test must redirect the ledger/baseline path it writes to — setting `AVA_HOME` alone is a silent no-op once `settings` has been imported, and fixture names leaked into the real ledger |
| `test_alloc_lease.py` | Planner decision table (priority, pinned, speculative levers, shortfall), `flock` ownership incl. a forked dead-holder reclaim with no timeout, and advisory mode touching no driver |
| `test_alloc_measurement.py` | Convention guard: free-memory reads go through the `hwinfo` HAL (only `alloc/capacity.py` calls it) — the misleading sources are named and forbidden |
| `test_alloc_watch.py` | Allocation watchdog: a running-but-not-loaded model raises a critical alert, unfit is silent until it persists, undeclared memory only alerts when it blocks something, audit records transitions only, dormant until models are declared |
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
| `test_agent_brain.py` | The agent sandbox's model is the operative brain — surfaced truthfully. |
| `test_alloc_deferred.py` | A deferred acquire: answer now, make room in the background. |
| `test_alloc_driver_template.py` | The driver template in docs/ALLOCATION.md must produce a driver that WORKS. |
| `test_alloc_recovery.py` | What the allocator promises, and what survives a reboot or a crash. |
| `test_app_appearance.py` | Connected-app rail identity: the auto-varying icon + the Hub's appearance picker endpoint. |
| `test_ava_mcp_adapter.py` | sdk/host/ava_mcp — the adapter that turns an ava-tools/1 facade into a REAL MCP server. |
| `test_ci_covers_deploy.py` | CI must exercise the install path README.md recommends first. |
| `test_cli_entrypoint.py` | The `ava` command must exist, and mean one thing. |
| `test_code_applier_policy.py` | The decision layer that gates an LLM writing to your source tree. |
| `test_code_tool_policy.py` | The code agent may not READ what it may not write. |
| `test_config_template_sync.py` | config.example.yaml is the only documentation of what Ava can be configured to do, so a key the code reads and the template omits is a knob nobody… |
| `test_connector_codegen.py` | The .mjs the connector generators emit must be valid JavaScript. |
| `test_connector_scaffold.py` | `ava connector new` must scaffold something that WORKS when you follow it. |
| `test_connector_secrets.py` | Connector credentials: the VALUE lives server-side (secrets/env/<NAME>, 0600), keyed by the env-var NAME a manifest references — never in the… |
| `test_connector_transport.py` | Connector transport honesty guard. |
| `test_deploy_compose_contract.py` | A profile must start the service its own backend URL points at. |
| `test_deploy_refs_tracked.py` | Every deploy script a tracked file points at must itself be tracked. |
| `test_dockerfile_bind.py` | The bridge container must bind 0.0.0.0 inside its own network namespace. |
| `test_example_app.py` | examples/hello-app is the SDK's front door, so it has to actually work. |
| `test_feature_convention.py` | Convention guard: ALL features.* reads go through ava_bridge/features.py. |
| `test_features.py` | The optional-feature registry contract (ava_bridge/features.py). |
| `test_intent_eval.py` | Eval-tooling MECHANICS (Phase 3). Hermetic: regex routing mode (no model), a throwaway AVA_EVALS_DIR, cases built inline — NEVER shipped data.… |
| `test_internal_scopes.py` | Every /internal route must be classified, and the docs must not overclaim. |
| `test_media_preflight.py` | Image-render preflight: `features.image` is the single authoritative switch. |
| `test_media_retention.py` | `data.retention_days` must reach generated media and uploads. |
| `test_model_flags_ssot.py` | vLLM's per-model flags are resolved in exactly ONE place: deploy/model-flags.conf. |
| `test_module_boundaries.py` | A name is private or it is shared — it cannot be both. |
| `test_network_boundary.py` | Who is asking, over what, and may they claim an unowned Ava. |
| `test_no_blocking_routes.py` | An `async def` route may not do blocking work on the event loop. |
| `test_no_eval_data.py` | Standing decision (docs/dev/INTENT_ROUTING_AND_RELIABILITY_PLAN.md, Phase 3): the product ships eval TOOLING only — never an eval dataset. No seed… |
| `test_no_owner_identity.py` | Nothing tracked may carry the owner's identity, a private sibling app, an absolute home path, or a proprietary-tool watermark. |
| `test_password_change.py` | Changing the admin password from inside the product, and revoking sessions. |
| `test_path_roots.py` | There is exactly ONE resolver for where runtime state lives: `ava_bridge.settings`. |
| `test_settings_errors.py` | A broken ava.yaml must be loud, and must never be overwritten. |
| `test_settings_overrides.py` | Env-override honesty helpers (settings.env_override / explicitly_false). |
| `test_setup_wizard_flow.py` | The first-run wizard must not lie, and must not lock you out. |
| `test_skill_frontmatter.py` | Every skill the agent ships must be surfaceable in the UI. The Agent tab auto-discovers skills from their SKILL.md frontmatter… |
| `test_skills_registry.py` | Skills registry mechanics (ava_bridge/skills.py): discovery, frontmatter derivation, tool extraction, and the deploy-state signal — all against a… |

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
  **One seam per destination, not one per subsystem**: allocation writes to
  *three* places, and redirecting the ledger covers only the first —
  `ledger._dir` (leases, model state, breaker), `broker._log_path`
  (`logs/alloc.jsonl`, the decision record an operator reads before enabling
  enforcement), and `capacity._baseline_path`. Each has leaked into a live box
  at least once.
- **Own the threads you start** — a seam only holds while the patch is active,
  and a deferred acquire actuates on a worker thread that can outlive `setUp`'s
  cleanup. Join it (`broker.wait_for_actuations()`) from an `addCleanup`
  registered *after* the patches, so it runs before they are undone. A static
  guard cannot catch this: the file patches everything correctly and still leaks.
- **A guard only covers tracked files** — every convention check resolves its
  inputs with `git ls-files`, so a brand-new file is invisible to all of them
  until git knows about it (`git add -N` is enough). The whole allocation layer
  once sat untracked, and its guards passed over an empty file list.
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
