# Install reference

This is the long half of installing Ava. It is for you if one of these is true:

- Ava is already running and you now want to change something - the model, the
  memory share, the engine, where the GPU shows up.
- You need a path the one-line installer does not cover: bare metal, Apple
  Silicon, a fork, or picking the container set by hand.
- Something did not behave and you want to know what the installer actually did.

If you have not installed yet, start with the
[Quickstart](../deploy/README.md) instead. It is one command, and it writes
everything below for you.

A **profile** here means which set of containers Ava starts. **Provisioning** a
capability means switching it on and giving it the service it needs.

---

## 1. Pick the profile yourself

The installer detects your hardware and picks one. To choose instead, copy a
profile file to `.env` and start. The selection lives in the file, so every later
`logs` / `down` / `pull` sees the same settings:

```bash
cd deploy
cp profiles/cpu.env   .env   # no GPU  -> Ollama for inference
cp profiles/cuda.env  .env   # small NVIDIA GPU -> Ollama with CUDA (quantized)
cp profiles/gpu.env   .env   # NVIDIA GPU, 12 GB or more -> vLLM
cp profiles/rocm.env  .env   # AMD GPU or APU -> Ollama with ROCm
cp profiles/cloud.env .env   # bring an API key, no local model (edit .env first)
cp profiles/agent.env .env   # + full tool-using agent (opt-in)

docker compose up -d
```

`install.sh` takes the same choice as an environment variable, which is the form
the landing page points people at:

```bash
cd deploy
AVA_PROFILE=agent ./install.sh     # cpu | cuda | gpu | rocm | cloud | agent
```

Given one, `install.sh` skips detection and uses it; given none, it detects and
prints what it chose and why. What each starts (`docker compose config
--services`, not a description of it):

| Profile | Containers | What that buys |
|---|---|---|
| `cpu` | ava, ollama | chat on a local model, no GPU |
| `cuda` | ava, ollama-cuda | chat on a local model, on a small NVIDIA GPU |
| `gpu` | ava, vllm | chat on a local model, NVIDIA |
| `rocm` | ava, ollama-rocm | chat on a local model, AMD |
| `cloud` | ava | chat against an API key you supply |
| `agent` | ava, agent, vllm | **+ the tool-using agent** that drives your connected apps |

So `agent` is the superset - everything `gpu` runs, plus the agent.

!!! warning "`agent` grants root-equivalent access to your machine"
    The agent container mounts the host Docker socket (`/var/run/docker.sock`),
    which is **root-equivalent** on the host. That is how it spawns the sandbox
    that runs model-generated code. The profile is opt-in for that reason: it
    is your call to make rather than the installer's.

Voice is not a profile at all - it needs a build flag (`AVA_VOICE_DEPS=1`) *and*
the switch in Setup → System. See [Optional capabilities](#4-optional-capabilities) below.

??? note "What each profile file sets, and why the profile lives in `.env`"
    Compose reads `COMPOSE_PROFILES` from `.env`, so selecting the profile *in the
    file* means every later command - `logs`, `down`, `pull`, `exec` - sees the same
    environment as `up` did. `--profile` is per-invocation: a `docker compose down`
    typed without it silently interpolates different values and can fail to find the
    services it is meant to stop.

    It also fixes the defect these files were added for. The `ava` service starts
    under every profile, so its `AVA_BACKEND_URL` could only have one global default
    (`http://vllm:8002/v1`) - and under `--profile cpu`, where no `vllm` service
    exists, that pointed the bridge at a hostname nothing would ever answer. The
    backend settings live with the profile that provides them now, and compose
    refuses to start rather than interpolate a default that cannot be right for
    every profile.

    | Profile file | Serves inference with | Notes |
    |---|---|---|
    | `cpu.env` | Ollama, on the CPU | Slow but starts anywhere. `install.sh` picks this for a box with no GPU, for one whose largest GPU has under 4 GB of VRAM, and for one whose Docker daemon cannot reach the card. |
    | `cuda.env` | Ollama with CUDA, on an NVIDIA GPU | Quantized GGUF weights (`llama3.2`, Q4_K_M, about 2 GiB) rather than FP16, so it fits where vLLM does not. `install.sh` picks this between **4096 MiB** and **12000 MiB** of VRAM - the 6-8 GB laptop cards that used to be sent to `cpu`. Needs the NVIDIA Container Toolkit, same as `gpu`. |
    | `gpu.env` | vLLM on an NVIDIA GPU | Sizing depends on the model you name in `AVA_MODEL` — a 7B at BF16 needs ~18 GB of VRAM once a 32k KV cache is counted at the default 0.90 memory share. Also needs the NVIDIA container toolkit installed (`nvidia-smi` alone is not enough - the daemon needs the `nvidia` runtime, which `install.sh` checks for). |
    | `rocm.env` | Ollama with ROCm, on an AMD GPU or APU | Needs the amdgpu kernel driver loaded (so `/dev/kfd` and `/dev/dri` exist) and your user in the `render` and `video` groups. `install.sh` checks both and falls back to `cpu` rather than starting a service that cannot see the device. **Not verified on real AMD hardware** - the `linux-amd-*` rows in `deploy/platforms.conf` read `ci-simulated`. |
    | `cloud.env` | Someone else's API | **You must fill in three values** - the file ships them empty on purpose, so compose stops with an instruction instead of guessing. |
    | `agent.env` | vLLM + the tool-using agent | Sets `COMPOSE_PROFILES=agent,gpu`, because `agent` alone starts no inference engine. Also sets the three variables the agent runtime needs. |

---

## 2. The model, and the knobs that size it

**Ava ships no default model.** You choose one with `AVA_MODEL=...` (gpu
profile) or via `ava.yaml`, and until you do there is no brain — chat says so
and links to Setup → Agent → Brain. A model Ava picked would be a model you did
not, and the phantom backend that used to appear when nothing was configured is
exactly what this avoids.

For scale: a 7B at BF16 is 14.2 GiB of weights and needs ~18 GB of VRAM once a
32768-token KV cache is counted at the default 0.90 memory share. `install.sh`
sizes your card in four tiers and tells you what fits; each threshold is
arithmetic the script spells out in a comment rather than a remembered number:

| Detected GPU memory | What the installer does |
|---|---|
| under 4096 MiB | falls back to the `cpu` profile - too little to hold even a quantized 3B on the card |
| 4096 to 12000 MiB | switches to the `cuda` profile: Ollama with CUDA on quantized GGUF weights, which fit where FP16 does not. Falls back to `cpu` if Docker has no `nvidia` runtime registered |
| 12000 to 18000 MiB | keeps vLLM and serves a ~3B model instead (same tool parser, same 32k context) |
| 18000 MiB or more | serves the default |

Pin `AVA_MODEL=` to override any of that.

The context is not the lever to reach for on a small card: 32768 is chosen to
clear the ~29k tokens Ava's own system prompt and tool schemas occupy, so
shortening it breaks the agent before it saves you any memory. Scaling up is the
deliberate act:

| Variable | Default | Notes |
|---|---|---|
| `AVA_MODEL` | *(empty — required)* | Any model vLLM can serve; Ava ships no default. Set it, then re-run `./install.sh` (or `./resolve-model-flags.sh --env <model> >> .env`) so its parsers follow. |
| `AVA_VLLM_GPU_UTIL` | `0.90` | Fraction of GPU memory vLLM may take. Lower it if you keep a second model resident on the same card. |
| `AVA_VLLM_MAX_LEN` | resolved | Context ceiling, **clamped to what the model actually supports** - vLLM raises rather than clamping, so asking for more than the checkpoint allows means it never boots. |
| `AVA_VLLM_MODEL_FLAGS` | resolved | `--tool-call-parser`, `--reasoning-parser` and any model-specific boot flags, as one string. |

### Capping how much memory Ava may use

The knobs above size the *engine*. To bound what the whole install may take, set
a ceiling on the container itself and Ava sizes its recommendation from that
rather than from the machine:

```yaml
# deploy/docker-compose.override.yml
services:
  ava:
    mem_limit: 14g        # or `deploy.resources.limits.memory` under swarm
  vllm:
    mem_limit: 24g
```

Ava reads the cgroup ceiling (v2 then v1), reports it as the usable pool, and
**Setup → Hardware** says the number is the container's share rather than the
machine's. Verified on a 121 GB host: `-m 6g` recommends `tiny`, `-m 14g`
recommends `small`, `-m 48g` recommends `large`.

Two caveats worth knowing before you reach for it:

- A ceiling is not a reservation. It caps the container; it does not set memory
  aside, and a model that does not fit is OOM-killed rather than refused.
- It bounds **RAM**, not VRAM. A discrete card's pool is untouched by
  `mem_limit`, which is why Ava does not clamp a VRAM reading with one. Use
  `AVA_VLLM_GPU_UTIL` for the card.

On Windows the outer ceiling is WSL2's, not Docker's - see "On Windows, Ava runs
in a Linux container" on the [Quickstart](../deploy/README.md). To govern which
*models* may hold memory, and what yields to what, that is the `alloc` block in
`ava.yaml` rather than a container limit.

### Telling Ava about memory it cannot measure

The opposite problem. Ava measures its own pool and **withholds** a tier when it
cannot see the machine the models actually run on - an engine on the host while
Ava is in a container being the case that forced it (§6). That is the right
default, and an owner who knows their machine needs a way to say so:

| Where | Value |
|---|---|
| **Setup → Hardware** | "Memory Ava plans for" - set it, change it, or clear it back to measured, any time. No restart. |
| `ava.yaml` | `hardware.fit_memory_gb: 32` |
| Environment | `AVA_FIT_MEM_GB=32` (wins over `ava.yaml`, so Setup shows it read-only and points you at where it is set) |

Blank restores the measured reading. Values outside 1-4096 GB are refused rather
than clamped, because a silently corrected number is one you cannot see is wrong.

**It is advice, not actuation.** The value reaches the tier recommendation and
stops there. It deliberately never reaches `alloc`, which keeps deciding on
memory it has actually measured - so a wrong number here costs you a poor model
suggestion, never a released model. Setup also warns when you state *more* than
the box measures: understating is cautious, but overstating raises the tier past
what the machine can hold, and a model that does not fit is OOM-killed on load
rather than refused.

You no longer pick a parser by hand. `deploy/model-flags.conf` maps a model to
its parsers and real context length, and `deploy/resolve-model-flags.sh` is the
only thing that reads it - `install.sh`, `local-serve.sh` and compose all go
through it, so the container and bare-metal paths cannot disagree. A parser
that does not match the model returns no `tool_calls` *silently*, and every
turn then runs to timeout, so this is the one setting worth getting right.
An unknown model family serves without tool-calling rather than guessing.
See [Pick a model](CHOOSE_A_MODEL.md).

---

## 3. Inference backend wiring (Docker)

Chat flows bridge → embedded router (`:8010` in-container) → the profile's
engine. `AVA_BACKEND_URL`/`ENGINE`/`MODEL` come from
`deploy/profiles/<profile>.env`, which you copy to `deploy/.env`. Compose has
**no default** for them and refuses to start without them: the bridge runs
under every profile, so any one default is wrong for the others - the old
global `http://vllm:8002/v1` pointed the `cpu` profile at a service it never
started. For `cloud`, `AVA_BACKEND_URL` and `AVA_MODEL` ship empty (`ENGINE` is
already `openai`), as does `AVA_INFERENCE_KEY` - which is *not* start-guarded,
so an empty key starts cleanly and fails on the first turn instead.

---

## 4. Optional capabilities

### Voice

The default image ships `ffmpeg` for audio handling, but not the speech models,
which are ~2 GB of torch/whisper/speechbrain. Voice enrollment and transcription
both need those, so **neither works on the default image** - build with them by
setting `AVA_VOICE_DEPS=1` in `deploy/.env` before `docker compose up -d
--build`, then turn the capability on in **Setup → System → Optional features**
(`features.voice` is off by default). Both steps are needed: the switch alone has
nothing to run, and the deps alone leave the switch off.

### Web search

No profile provisions SearXNG or Tor, so this does **not** work out of the box in
Docker. `AVA_WEB_SEARXNG_URL` defaults to `http://127.0.0.1:8888`, which inside
the `ava` container is the *container's* loopback - not your host's - so a
SearXNG you run on the host is not reachable at that address either. To wire it
up: run SearXNG yourself, then set `AVA_WEB_SEARXNG_URL` to an address the
container can actually reach (a compose service name, or
`http://host.docker.internal:8888` with the matching `extra_hosts`). Host-side
fetch stays fail-closed over Tor by default, so leaving Tor unprovisioned means
fetch errors rather than clearnet requests - set `AVA_WEB_TOR=0` to accept direct
egress instead. **Egress** here means traffic leaving your machine.

---

## 5. Surfacing the GPU in the bridge container

Compose grants the GPU to the *inference* service only, so **Setup → Hardware**
reports no GPU even on an NVIDIA box, and sizes its model tier from system memory
instead. The panel says so rather than implying a driver fault. To surface the
real card, drop a `deploy/docker-compose.override.yml` next to the compose file:

```yaml
services:
  ava:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: ["utility"]   # telemetry only - no compute claimed
```

Compose merges `docker-compose.override.yml` automatically. It is opt-in rather
than shipped because a `devices:` reservation makes compose **refuse to start at
all** on a host without the NVIDIA container runtime - which is every `cpu` and
`cloud` install, and every Mac.

---

## 6. Pointing Ava at an engine on the host machine

Ava looks for an engine in three places, in this order: the compose network
(`vllm:8002`, `ollama:11434`), its own loopback, and - when it is running in a
container - **the machine that container runs on**, via the runtime's host
gateway (`host.docker.internal` under Docker Desktop, `host.containers.internal`
under Podman). A natively-installed Ollama or LM Studio is therefore a supported
way to run Ava, not a workaround.

On Windows and macOS it is often the better arrangement. A native engine gets the
real GPU and the machine's full memory; the WSL2 VM Ava runs in has neither.

**One thing to set.** Most engines listen on `127.0.0.1` only, which no container
can reach:

| Engine | What to set |
|---|---|
| Ollama | `OLLAMA_HOST=0.0.0.0`, then restart it. On Windows use the environment-variables UI; on a Mac, `launchctl setenv OLLAMA_HOST 0.0.0.0`. |
| LM Studio | Turn on **Serve on local network** in the server tab. |
| llama.cpp | `llama-server --host 0.0.0.0` |

Without it the engine is running and unreachable, which from inside the container
looks exactly like never having installed one - so Setup names that cause rather
than reporting nothing found.

!!! note "Ava stops recommending a model size when the engine is out there"
    **Setup → Hardware** can only measure its own container. That is not the pool
    a host engine draws on, so a tier derived from it would describe the wrong
    machine - "small" on a laptop whose native Ollama has the full 32 GB and a
    real GPU. Ava reports what it can see, says where the engine is, and declines
    to size a recommendation. Pick from what that engine already holds under
    **Agent → Brain**.

---

## 7. Bare metal: engine wiring and tool calling

The bare-metal install steps are on the
[Quickstart](../deploy/README.md), collapsed under "Install bare metal instead".
This is what happens after them.

Chat flows **bridge → router → your engine**. The OpenAI-compatible router
starts **inside `ava up` automatically** (embedded, `127.0.0.1:8010`), so you
never need a second service. An always-on standalone unit
(`uvicorn ava_router:app --host 127.0.0.1 --port 8010`) is detected at startup
and used instead. Declare your engine in `ava.yaml`:

```yaml
inference:
  primary: local
  backends:
    local:
      engine: ollama                      # vllm | ollama | llamacpp | openai
      base_url: http://127.0.0.1:11434/v1
      model: llama3.2
```

`ava models pull --auto` downloads a model sized to your hardware and prints
this stanza for you. `ava doctor` checks the route chat *actually* uses.

**Tool calling per engine** (matters for the full agent; plain chat works
regardless):

| Engine | Tool calls | Launch requirement |
|---|---|---|
| vLLM | native | `--enable-auto-tool-choice --tool-call-parser <parser for your model>` (both are required; wrong parser = tools silently return as prose) |
| Ollama | native | none (tool-capable models only) |
| llama.cpp | opt-in | `llama-server --jinja` with a tool-call chat template; otherwise declare `tools: none` on the backend |
| cloud (openai) | native | none |

**Exposing the router beyond localhost**: set `inference.router.host: 0.0.0.0`.
Every `/v1/*` call then requires the router token
(`$AVA_HOME/secrets/router_token`) as a `Bearer` / `X-Ava-Router-Token`
header. Loopback (the default) needs no token for `/v1/*`.

---

## 8. Apple Silicon (Mac mini / Studio)

A Mac is **unified-memory** hardware: CPU and GPU share one RAM pool and there is
no `nvidia-smi`. **Ava does not serve vLLM on a Mac** - upstream macOS support is
experimental, build-from-source and CPU-only unless you add the community
`vllm-metal` plugin. Run bare metal with a native engine (Ollama, llama.cpp, MLX,
LM Studio) so inference uses the Metal GPU. The hardware layer detects
Apple Silicon automatically and gates model routing on the shared RAM pool (a
512 GB Studio runs a 70B comfortably; a 24 GB mini should stay ~8B).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt              # no CUDA/vLLM wheels; clean on arm64

# a native, OpenAI-compatible engine (any of these work; Ollama shown):
brew install ollama && ollama serve &
ollama pull llama3.1:70b                      # sized to YOUR Mac's memory

./bin/ava setup      # on a Mac this seeds an Ollama backend, not the vLLM default
./bin/ava doctor     # confirms it detects Apple Silicon + the right memory tier
./bin/ava up         # http://localhost:8096
```

Notes:

- **`ava models pull --auto` is Apple-aware** - on a Mac it fetches an Ollama
  model sized to your memory, never a CUDA-only vLLM model, which cannot be
  served on Apple Silicon.

- LM Studio and MLX also work - point the backend `base_url` at their
  OpenAI-compatible endpoint (see the Apple example in
  [`config.example.yaml`](../config.example.yaml)).

- GPU **memory** shows in the dashboard; util/temp/power read blank on Apple
  (no unprivileged API) - that is expected, not a fault.

---

## 9. Configuration: `ava.yaml` and secrets

Everything is driven by **`$AVA_HOME/ava.yaml`** (copied from
[`config.example.yaml`](../config.example.yaml) by `ava setup`) plus environment
overrides. **No source edits, ever.** Highlights:

| Setting | What it does |
|---|---|
| `server.port` | web app port (default 8096); env `AVA_PORT` |
| `inference.backends` | your model engines (vLLM / Ollama / llama.cpp / cloud) |
| `agent.sandbox` | the agent runtime sandbox name |
| `features.*` | `web_search`, `voice`, `branding`, `memory` (Setup → System → Optional features); `memory` also has its own detail panel |
| `connectors` | the apps Ava monitors and drives |

Secrets live outside `ava.yaml` and outside the repo: the admin password in
`$AVA_HOME/data/auth_password`, the session signing key in
`$AVA_HOME/data/.secret`, the router token and cloud API key under
`$AVA_HOME/secrets/` - or supply any of them by env (`AVA_PASSWORD`,
`AVA_SECRET`, `AVA_ROUTER_TOKEN`, `AVA_INFERENCE_KEY`). Full inventory:
[SECURITY.md §4](../SECURITY.md).

---

## 9a. What actually runs, and on which port

Ava is not a fleet. On bare metal it is **one process**, plus whatever the agent
runtime brings with it:

| Process | What it is | Default port | Set by |
|---|---|---|---|
| the bridge | `phone_bridge.py` under uvicorn — the whole app: UI, chat, voice, connectors | `127.0.0.1:8096` | `server.port` / `AVA_PORT` |
| the inference router | started **inside** the bridge, not a second service | `127.0.0.1:8010` | `inference.router.port` |
| the OpenShell host gateway | NemoClaw's policy plane, started by `nemoclaw onboard` | `127.0.0.1:8080` | `nemoclaw onboard` |
| the OpenClaw gateway | the agent's control plane, only with `agent.runtime: openclaw_gw` | from the sandbox registry | `nemoclaw onboard` |

Everything binds **loopback**. Reaching Ava from a phone is Tailscale's job, not
a wider bind — see [SECURITY.md §1](../SECURITY.md).

### Units

The bridge itself is yours to supervise however you like; `deploy/install.sh`
writes a user unit for it. The one unit Ava generates for you is the **host
gateway's**, because that process has no supervisor of its own and vanishes on
reboot — see ["Surviving reboots"](AGENT_RUNTIME.md#surviving-reboots):

```bash
ava agent install-units          # report what it would write
ava agent install-units --write  # install it
```

Two host-level scripts ship but are **not** installed for you, because both
touch things outside `$AVA_HOME`: `deploy/nemoclaw-boot-recover.sh` restores the
sandbox's port forwards, and `deploy/ava-sandbox-firewall.sh` re-adds the
`INPUT` ACCEPT rules a sandbox needs on a host that defaults to DROP.

`ava doctor` reports all of it, including the gateway's phase and version, for
whichever runtime is actually serving turns.

> There is a fuller per-machine inventory in `docs/dev/COMPONENTS.md`. It is
> **gitignored on purpose**: it maps one operator's box — their other services,
> their ports, their paths — and would be a wrong map for anyone else. What a
> fork needs is the table above.

## 10. Installing from a fork

Installing **from a fork**, without cloning it first? Point `AVA_REPO` at your
fork and pipe its own copy of the installer (it is the same script the
[Quickstart](../deploy/README.md) runs):

```bash
# replace `master` with your fork's default branch or a release tag:
AVA_REPO=https://github.com/<you>/ava.git bash -c "$(curl -fsSL https://raw.githubusercontent.com/<you>/ava/master/deploy/install.sh)"
```

Verifying a fork's own signed build is covered under "Verify the signed image"
on the [Quickstart](../deploy/README.md).

---

## 11. Running a second instance (a staging slot)

A **slot** is a second Ava running beside the first, with its own database, its
own port and its own image tag, so a build can be tested while the stable
instance keeps serving. It is the same compose file, layered with
`deploy/slot.yml` and run as a separate compose project:

```bash
cd deploy
./slot.sh init              # writes .env.staging from your working .env
./slot.sh up -d --build     # builds the working tree into the slot
./slot.sh down              # ALWAYS before `docker compose down` on stable
```

Then open **`http://127.0.0.1:8097`**. Everything else is passed through to
`docker compose`, so `./slot.sh logs -f ava` and `./slot.sh ps` work as usual.

**Go through `slot.sh` rather than typing the compose command.** A slot separates
four things, and each one is silent when it breaks: the compose project (without
`-p`, `up` recreates the *stable* containers), `AVA_HOME` (two bridges over one
SQLite store and one session-signing key), the published port, and `AVA_IMAGE` -
which is both the build output tag and the run reference, so a slot built under
the default tag retags the image the stable stack restarts into. The wrapper pins
all four on every subcommand and refuses to start if any of them still matches
stable. It is the same argument [deploy/profiles/README.md](../deploy/profiles/README.md)
makes for keeping `COMPOSE_PROFILES` in a file.

!!! warning "Use `127.0.0.1` for the slot and `localhost` for stable - not both"
    Cookies are blind to port. Ava's session cookie is host-only with no `Domain`
    attribute, so signing in to a slot at `localhost:8097` **overwrites the stable
    stack's cookie at `localhost:8096`**. Each instance signs with its own key, so
    stable then rejects the cookie and shows the sign-in page - which reads
    exactly like a session expiry, from an action that had nothing to do with it.
    `localhost` and `127.0.0.1` are distinct cookie hosts and both are already
    trusted, so keeping one per instance costs nothing and closes it.

**The slot borrows stable's engine.** `.env.staging` sets `COMPOSE_PROFILES=`
empty, so no engine container starts; `slot.yml` joins the stable project's
network instead, and `AVA_BACKEND_URL` stays exactly what stable uses
(`vllm:8002`, `ollama:11434`). No second copy of the weights, and no second claim
on the GPU - which on a memory-capped box is the difference between working and
not. The cost is that the slot must come **down first**: `docker compose down` on
stable cannot remove a network the slot still holds an endpoint on.

This is also why the slot does not use `host.docker.internal`. §6 above is about a
host-native engine listening on `0.0.0.0`; a compose engine is published on
`127.0.0.1` only, so its forwarding rule is bound to loopback while the host
gateway address is not, and current Docker no longer routes between the two. The
slot would boot healthy, pass its healthcheck, serve the UI, and then refuse every
chat turn with an error that reads like a model fault.

!!! warning "A slot is a second, invisible tenant on stable's engine"
    Its brain display is honest - that model really is what would answer a turn
    here - but the memory it reports is not the slot's to free, and turns you run
    in the slot consume production's engine. `.env.staging` therefore sets
    `AVA_ALLOC_INFER=0`, which keeps residency **observable** while removing the
    unload lever: without it, "Memory Ava can free" renders on every screen of the
    slot and one click evicts the model out from under production chat. See
    [ALLOCATION.md](ALLOCATION.md) for what that lever normally does.

**Telling them apart.** `slot.sh up` stamps the build it makes as
`<version>+stg.<sha>`, plus `.dirty` when the working tree has uncommitted
changes, and that string is what **Setup → System → About** shows. This is the
only thing that distinguishes a local build from a release: a plain
`docker compose build` leaves `AVA_VERSION` empty, so the image reports the same
version number the signed release does. `.env.staging` also sets
`AVA_NAME=Ava (staging)`, which labels every screen - but comment it out when the
slot is testing persona, prompt or agent behaviour, because `AVA_NAME` is injected
into the system prompt, not merely displayed.

!!! note "Two things the slot cannot check for you"
    `deploy/slot.yml` is passed with an explicit `-f`, and that **suppresses
    compose's automatic pickup of `docker-compose.override.yml`** (§5). A slot
    deliberately does not inherit it - it should not claim telemetry devices out
    from under stable - but the omission is silent.

    A slot also cannot catch **frontend `dist` drift**. The image rebuilds the SPA
    from `src`, so the slot happily shows your change with a stale committed
    bundle; only CI compares the two. Run `npm run build` in `frontend/` and commit
    the result before pushing.

Slots are named: `AVA_SLOT=perf ./slot.sh up -d` uses `.env.perf` and the project
`ava-perf`. Two at once need different `AVA_PORT_HOST` values.

---

Back to the funnel: [Step 1: Install](../deploy/README.md) ·
[Step 2: Pick a model](CHOOSE_A_MODEL.md)
