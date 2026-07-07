# Switchover runbook — make open-model 30B Ava's always-on brain

Ava's app is already rewired to the single **Nemotron open-model 30B-A3B (FP8)**
backend (`nvidia/Nemotron-Open-30B-A3B-Reasoning-FP8`, served at `:8002`,
fronted by `ava-router` `:8010`). This runbook is the **serving switch** — the
one step left, which touches the host `vllm.service` / Docker and therefore needs
`sudo`. It was deliberately left for you to run, because the retired Super/Nano
containers are **shared** with your other apps + IDE Copilot.

> **Hard constraint:** the Omni model (~35 GB, FP8) and the Super 120B (~85 GB) cannot
> both be resident in the 121 GB unified pool. Bringing Omni up **requires**
> stopping Super (and Nano). Anything still pointing at Super/Nano (your other apps,
> Copilot BYOK) will lose that endpoint until you repoint it (their repos, separate).

## 0. Preconditions

- [ ] Model downloaded: `~/ai/models/_hf/hub/models--nvidia--Nemotron-Open-30B-A3B-Reasoning-FP8` is ~35 GB and complete.
- [ ] You've decided your other apps/Copilot can lose Super/Nano (or you'll repoint them to `:8002` Omni afterwards).

## 1. Bring up the always-on model container

The self-contained launcher stops the retired containers and starts `vllm-open`:

```bash
bash ~/projects/Ava/deploy/omni-serve.sh
```

This runs `docker run --restart unless-stopped … --name vllm-open -p 8002:8000`
against `vllm/vllm-openai:v0.20.0-aarch64-cu130-ubuntu2404`, mounting the `_hf` hub. It waits for
`/health` and prints readiness.

## 2. Validate serving BEFORE relying on it

```bash
# Model is listed:
curl -s localhost:8002/v1/models | python3 -m json.tool

# A real completion returns text (this is the true "vLLM can serve it" check):
curl -s localhost:8002/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "nvidia/Nemotron-Open-30B-A3B-Reasoning-FP8",
  "messages": [{"role":"user","content":"Say hello in one short sentence."}],
  "max_tokens": 40 }' | python3 -m json.tool
```

**Tool-call parser — CORRECTED 2026-07-06.** The parser MUST be
`--tool-call-parser qwen3_coder` (per the model card's vLLM section), NOT
`hermes`. Omni emits tool calls in the qwen3_coder XML format
(`<function=name><parameter=x>…`); `hermes` expects JSON, so with `hermes` vLLM
returns **no `tool_calls`** — the call leaks through as plain text, the OpenClaw
agent never sees a tool call, and every tool-using turn (e.g. weather) loops on
reasoning until it times out. Symptom: Ava "never responds" to simple questions.
`--reasoning-parser nemotron_v3` is correct. Both `deploy/omni-serve.sh` and the
`~/ai/models/REGISTRY.yaml` omni entry now use `qwen3_coder`. Do NOT "just drop
the flags" as a fix — without a tool-call parser, tool calls are never parsed and
the same loop happens. If vLLM ever rejects the name, re-check the model card.

**If it OOMs under a concurrent the GPU service render:** lower `OMNI_GPU_UTIL` (e.g.
`OMNI_GPU_UTIL=0.55 bash deploy/omni-serve.sh`) or `OMNI_MAX_LEN=16384`.

## 3. Point Ava at it (already done in config — just confirm)

Ava's router already defaults to the Omni backend. Confirm end-to-end:

```bash
# Router sees the omni backend and reports fit:
curl -s -H "X-Ava-Router-Token: $AVA_ROUTER_TOKEN" localhost:8010/fit | python3 -m json.tool
# Ask Ava something in the chat UI; the model pill should read "open-model 30B".
```

## 3b. Point the SANDBOX AGENT at Omni too (the piece this runbook missed)

Ava's **app/router** (§3) and the **OpenClaw agent inside the NemoClaw sandbox**
have *separate* inference configs. Repointing the router is not enough — the
agent (tools/turns) has its own model set during `nemoclaw onboard`, and it was
still on Super-120B, so agent turns 400'd/hung. Two things to fix:

1. **Model id** — replace the Super id with the Omni id in the agent's config.
   Authoritative file: `~/.openclaw/openclaw.json` in the sandbox (+ its
   `.last-good`); also the derived `agents/main/agent/models.json`, and the host
   records `~/.nemoclaw/{onboard-session.json,sandboxes.json}`. Replace each
   `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4` →
   `nvidia/Nemotron-Open-30B-A3B-Reasoning-FP8` and set `contextWindow`
   to the served `--max-model-len`. (Supported alternative: a fresh
   `nemoclaw onboard` pointing at the Omni endpoint.)
2. **Context length** — the agent's system context (persona + 7 MCP tool servers'
   schemas) is **~29k tokens**. Omni MUST be served with `--max-model-len` well
   above that (Super had 131072). Serve Omni at **65536** (now the default in
   `deploy/omni-serve.sh`); at 32768 the prompt + output overflow and vLLM 400s.

Verify: `python -c "from ava_bridge import agent; print(agent.ask_openclaw('capital of France?', session_id='t'))"`
returns real text in a few seconds. (`nemoclaw status` may still *display* the old
model name — that's a cached gateway label; the actual turns use Omni.)

## 4. Make it survive reboots (always-on)

`--restart unless-stopped` already brings `vllm-open` back on Docker/host restart.
For a fully managed always-on setup, repoint the host **`vllm.service`** at the
Omni launcher instead of the old multi-model `start-vllm.sh`:

```bash
sudo systemctl edit --full vllm.service     # point ExecStart at deploy/omni-serve.sh
sudo systemctl daemon-reload
sudo systemctl restart vllm.service
sudo systemctl status vllm.service
```

Alternatively, edit your model-serving script (e.g.
`~/projects/<your-app>/deploy/scripts/start-vllm.sh`) to
replace the `vllm-super` + `vllm-nano` blocks with the `vllm-open` block from
`deploy/omni-serve.sh` (keep `vllm-embed` if you use it), then
`sudo systemctl restart vllm.service`.

## 5. Guardrail

`ava_security_check.py` now enforces the Omni-only invariant: it errors if a
retired `vllm-super`/`vllm-nano` container is running **alongside** `vllm-open`
(they exceed the memory pool together). Run it after the switch:

```bash
python3 ~/projects/Ava/ava_security_check.py
```

## Rollback

The old models are still on disk and in `REGISTRY.yaml` (status `available`). To
revert: `docker stop vllm-open && docker rm vllm-open`, then restart the original
`start-vllm.sh` flow (`sudo systemctl restart vllm.service`).
