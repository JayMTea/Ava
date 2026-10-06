# runtime/ — reference runtime

Loads the platform config, enforces the policies and runs agents. Small on purpose: read
it end to end before changing it. App code imports `runtime.Platform` and nothing deeper.

| Module | Responsibility |
|---|---|
| `orchestrator.py` | `Platform`: wires every component; `run`, `resume`, `run_workflow`; delegation |
| `agent_runtime.py` | The loop: model turn → tool calls → results, under limits; checkpoints; resume |
| `context_engine.py` | System prompt (AGENT.md → instructions → skill catalog → delegates → rules) and first message |
| `model_router.py` | Route → fallback chain → provider; skips disabled / under-cleared providers |
| `providers/` | `anthropic.py`, `openai_compat.py`, `mock.py` — translate to and from `types.py` |
| `tool_registry.py` | Catalog of tools; per-agent effective set (listed ∩ granted), sorted |
| `tool_executor.py` | The single choke point for every tool call |
| `policy_engine.py` | Hard rules from `policies/*.yaml`: grants, approvals, paths, URLs, limits, redaction |
| `approval_manager.py` | Approvers (deny-all, allow-list, interactive) and the audit record |
| `skill_registry.py` | Skill catalog and `skill.load` (progressive disclosure) |
| `memory_manager.py` | Namespaced, permissioned, redacted memory (JSONL backend) |
| `task_manager.py` | Task and artifact records, validated against `schemas/` |
| `checkpoint_manager.py` | Per-turn history snapshots for resume |
| `sandbox.py` | Docker wrapping for `sandbox: required` tools |
| `mcp_bridge.py` | Imports MCP server tools as `mcp.<server>.<tool>` |
| `builtins.py` | `skill.load`, `memory.recall`, `memory.remember`, `task.delegate` |
| `telemetry.py` | Spans, metrics, audit log, logging - all redacted |
| `config.py` | Load, env-interpolate, schema-validate and cross-check all config |
| `types.py` | Provider-neutral data types; no SDK imports |

## Invariants

- No module except `providers/` imports a model SDK.
- Every tool call goes through `ToolExecutor.execute`.
- History is append-only; assistant turns carry provider `raw` blocks that are replayed
  verbatim to the same provider.
- Every error a model or tool can cause ends as a tool error result or a `failed` task,
  not an exception from `Platform.run`. Unexpected internal errors mark the task `failed`
  first, then propagate.
