# Deployment profiles

Pick one, copy it to `deploy/.env`, start:

```bash
cd deploy
cp profiles/gpu.env .env      # or cpu / cuda / rocm / cloud / agent
docker compose up -d
```

`deploy/install.sh` does this for you, and additionally resolves your model's
vLLM flags into the same file.

## Why a file and not `--profile`

Compose reads `COMPOSE_PROFILES` from `.env`, so selecting the profile *in the
file* means every later command — `logs`, `down`, `pull`, `exec` — sees the same
environment as `up` did. `--profile` is per-invocation: a `docker compose down`
typed without it silently interpolates different values and can fail to find the
services it is meant to stop.

It also fixes the defect these files were added for. The `ava` service starts
under every profile, so its `AVA_BACKEND_URL` could only have one global default
(`http://vllm:8002/v1`) — and under `--profile cpu`, where no `vllm` service
exists, that pointed the bridge at a hostname nothing would ever answer. The
backend settings live with the profile that provides them now, and compose
refuses to start rather than interpolate a default that cannot be right for
every profile.

## The files

| Profile | Serves inference with | Notes |
|---|---|---|
| `cpu.env` | Ollama, on the CPU | Slow but starts anywhere. `install.sh` picks this for a box with no usable GPU — under 4 GB of VRAM, or a card Docker cannot reach. |
| `cuda.env` | Ollama on an NVIDIA GPU | Quantized GGUF weights, so it fits where vLLM does not: `install.sh` picks it between 4 GB and 12 GB of VRAM. Needs the NVIDIA Container Toolkit. |
| `rocm.env` | Ollama on an AMD GPU | The AMD equivalent of `cuda.env`. ⚠️ not verified on device. |
| `gpu.env` | vLLM on an NVIDIA GPU | Needs ~18 GB VRAM for the shipped default model at the default 0.90 memory share (14.2 GiB of FP16 weights plus a 32k KV cache). `install.sh` picks it at 12 GB or more and downshifts to a 3B between 12 and 18 GB. |
| `cloud.env` | Someone else's API | **You must fill in three values** — the file ships them empty on purpose, so compose stops with an instruction instead of guessing. |
| `agent.env` | vLLM + the tool-using agent | Also sets the three variables the agent runtime needs, which used to be a manual step documented three sections away. |

## Changing the model

Set `AVA_MODEL`, then re-resolve its vLLM flags — a model's tool-call parser is
not optional, and a wrong one fails silently:

```bash
deploy/resolve-model-flags.sh --env <model-id> >> deploy/.env
```

`deploy/install.sh` does this automatically. See `docs/CHOOSE_A_MODEL.md`.
