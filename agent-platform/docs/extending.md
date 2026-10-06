# Extending the platform

Recipes for every kind of change. Each lists **every file that must change together** —
the validator (`python scripts/check.py --fast`) catches a missed cross-reference, but
following the list avoids the round trip. Finish every recipe with `python scripts/check.py`.

## Add an agent

1. `agents/<name>/agent.yaml` — copy an existing one. `name` equals the directory.
   - `description`: what it is for and *when to hand it work* (delegating agents choose
     from this text).
   - `model.route`: a route from `models/routing.yaml`, never a model id.
   - `tools`: only what the role needs. `skills` need the `skill.load` tool.
   - `data_classification`: the most sensitive data it touches.
2. `agents/<name>/instructions.md` — role, approach, output. Explain *why* behind any
   constraint; models follow reasons better than capitalised rules.
3. `policies/tool-permissions.yaml` — grant exactly the tools listed in step 1.
4. If another agent should delegate to it: add it to that agent's `delegates_to` (which
   requires that agent to have `task.delegate`).
5. `memory/namespaces.yaml` — if it reads or writes memory, permit it there.
6. `evals/datasets/` — at least three cases for it, one an edge case.
7. README agents table.

**Before adding an agent, ask whether a skill would do.** A new agent is justified by
different tools, model or permissions — not by a different procedure.

## Add a skill (for runtime agents)

1. `skills/<name>/SKILL.md` with frontmatter `name` (equals directory; lowercase-hyphenated,
   ≤ 64 chars) and `description` (≤ 1024 chars: what it does **and when to use it** — it is
   all the model sees until it loads the skill). Optional: `license`, `compatibility`,
   `metadata`, `allowed-tools`.
2. Body: the procedure. Keep it focused on one capability; move long reference material to
   `references/`, templates to `assets/`, helper code to `scripts/` and point to them by path.
3. Add the skill to each agent that should have it (`skills:` plus the `skill.load` tool).

## Add a tool

1. Implement `fn(args: dict, ctx: ToolContext) -> str | dict | list` in
   `tools/<family>/__init__.py`.
   - Resolve every path with `ctx.policy.resolve_path(path, "read" | "write")`.
   - Check every URL with `ctx.policy.check_url(url)` (and every redirect).
   - Never build shell strings; pass argv lists. Validate anything that could be parsed as
     an option (see `tools/git` `_ref`).
   - Raise `ToolError("message for the model")` for expected failures.
   - Return compact, useful data; the executor truncates at `max_output_chars`.
2. Register it in `tools/registry.yaml`: id `<family>.<action>`, a description written for
   the model, `side_effects` (classify honestly — it drives approvals), `input_schema` with
   `additionalProperties: false`, `timeout_s`, and `sandbox: required` if it executes code.
3. Grant it in `policies/tool-permissions.yaml` and list it in the agents that use it.
4. Tests in `tests/tools/`: the happy path **and** every refusal (bad path, bad host, bad
   argument). If it is a new safety boundary, add an `evals/scenarios/` case too.

## Add a workflow

1. `workflows/<name>.yaml` (name equals the file name). Steps run in order:
   - `agent:` + `input:` — run an agent on templated text.
   - `tool:` + `as_agent:` + `arguments:` — run a tool deterministically under that agent's grants.
   - `approval:` + `message:` — stop unless a human approves.
   - `continue_on_error: true` on steps whose failure should not stop the workflow.
2. Templates: `{{ inputs.<name> }}` and `{{ steps.<id>.output }}` (only earlier steps).
3. Tests in `tests/workflows/`, an eval case using `workflow:`, and an A2A route if exposed.

## Add or change a model

- New model on an existing provider: add a key in `models/registry.yaml` (`provider`,
  `model`, `max_output_tokens`, `supports_effort`, `default_effort`, `options`), then put
  it in route chains in `models/routing.yaml`.
- New route: add it to `routing.yaml`; point agents at it.
- New provider: `models/providers/<name>.yaml` (`type`: `anthropic` or `openai_compatible`
  covers most vendors and local servers), `enabled_when_env`, `api_key_env`, and an honest
  `max_data_classification`. A wire protocol that isn't covered needs an adapter in
  `runtime/providers/` implementing `ModelProvider`, registered in `PROVIDER_TYPES`, with a
  translation test like `tests/integration/test_routing_and_providers.py`.
- Before switching a production route, run the dataset evals on both routes and compare
  pass rate, tokens and cost per completed task (`evals/benchmarks/`).

## Change a policy

Make the smallest change that serves the need, and say in your summary what was widened.

- Grant a tool: add a rule `{agents: [...], tools: [...], reason: ...}` under `grants`.
- Allow a domain: add it to `network.yaml` `allow_domains` (it covers subdomains).
- Allow writes elsewhere: add a glob to `data-access.yaml` `write_allow`.
- Gate a tool: add its id (globs allowed) to `approvals.yaml` `require_approval.tools`.
- New secret format: add a `redaction.patterns` entry to `safety.yaml` and a test.

## Connect an MCP server

1. `mcp/servers.yaml` — transport (`stdio` with `command`/`args`, or `http` with `url`),
   secrets via `${VAR}`, `default_side_effects` (leave `external_write` until classified),
   then `enabled: true`.
2. `mcp/permissions.yaml` — `expose` the smallest useful set of its tools; `hide` the rest.
3. `mcp/adapters/<server>.yaml` — classify each exposed tool's `side_effects` honestly.
4. Agent: add the server to `mcp_servers` and `mcp.<server>.<tool>` (or `mcp.<server>.*`)
   to `tools`. Grant the same ids in `policies/tool-permissions.yaml`.
5. `pip install -e ".[mcp]"`.

## Enable shell execution

Only when an agent truly needs to run code. All of:

1. `docker build -t agent-sandbox:latest sandbox/` and set `sandbox/permissions.yaml`
   `mode: docker`; keep the `default` profile (no network, read-only) unless a task needs more.
2. Grant `shell.run` to one specific agent and remove the `"*"` deny for it — replace it with
   a deny that still covers every other agent.
3. Keep it under approval (`destructive` is gated by default).
4. Add eval scenarios proving it is refused for other agents and without approval.

## Add a memory namespace

`memory/namespaces.yaml`: `description`, `scope` (`global` / `session` / `agent`), `read`
and `write` agent lists, `ttl_days`. Then add it to the agents' `memory.read/write`. To swap
the storage backend (vector DB), reimplement the storage methods in
`runtime/memory_manager.py` and keep `can()` and redaction above them.

## Add an eval

- Quality case: one JSON line in `evals/datasets/*.jsonl` — `id`, `suites`, `agent` (or
  `workflow` + `inputs`), `input`, `graders`, and a `mock_script` so it also runs offline.
- Behaviour/safety case: a YAML file in `evals/scenarios/` that scripts the model to attempt
  the thing that must be refused, with graders on the outcome.
- Graders: `status`, `contains`, `not_contains`, `regex`, `tool_called`, `tool_not_called`,
  `tool_error_contains`, `llm_judge` (rubric; real routes only). Add new ones in
  `evals/graders/__init__.py`.
- Then `python -m evals.run --route test --update-baseline` and commit the baseline.

## Add a product surface

Put it in `app/`. Construct `Platform.load(root, approver=..., session_id=...)` and call
`run` / `run_workflow`. Map your user's identity to what they may do before calling; supply
an `Approver` that reaches a human through your UI. See `app/README.md`.
