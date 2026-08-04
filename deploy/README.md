# Install Ava on your hardware

<ul class="ava-steps" markdown="1">
<li class="is-now"><span><b>Step 1</b>Install</span></li>
<li markdown="span">[<b>Step 2</b>Pick a model](../docs/CHOOSE_A_MODEL.md)</li>
<li markdown="span">[<b>Step 3</b>Set up the agent](../docs/AGENT_RUNTIME.md)</li>
<li markdown="span">[<b>Step 4</b>Connect your apps](../docs/CONNECT_YOUR_APPS.md)</li>
</ul>

**What you end up with.** Ava running on your own machine at
`http://localhost:8096`, with your own admin password set. It is
**self-hosted and single-tenant**: your instance, your hardware, your models.

**How long.** About five minutes of your attention. The first run downloads
container images and model weights in the background, which is the slow part.
The installer waits up to three minutes for the app to answer, then tells you
whether it did.

**What you need first.** Docker, Docker Compose v2, and git. Plus disk for the
weights: the default NVIDIA model is `Qwen/Qwen2.5-7B-Instruct`, 14.2 GiB.

---

## 1. Run one command

The installer clones the repo, detects your hardware, picks a profile, resolves
your model's vLLM flags, and waits for the app to actually answer before saying it is done.

```bash
git clone https://github.com/JayMTea/Ava && cd Ava/deploy && ./install.sh
```

Already inside a clone? `cd deploy && ./install.sh` - the installer detects that
it is in a checkout and installs in place rather than cloning again.

**On Windows**, run these two lines in the Command Prompt window you already
have. Nothing to open first, no shell to choose:

```
git clone https://github.com/JayMTea/Ava
cd Ava\deploy && install.cmd
```

??? note "Why Windows gets its own command (and what the errors mean)"
    `install.cmd` finds the bash that ships with Git for Windows and hands it the
    same `install.sh` everyone else runs - there is no separate Windows installer,
    and no second code path to drift. Docker Desktop is the only other requirement.
    Environment knobs still work: `set AVA_PROFILE=gpu` before running it is passed
    through. You can also just double-click `install.cmd` in Explorer.

    **Why not `./install.sh` directly?** Because the shells Windows opens by
    default cannot run one. Command Prompt fails with `'.' is not recognized as an
    internal or external command`, and PowerShell cannot execute a bash script at
    all (5.1 also rejects `&&`). Both errors mean "wrong shell", not "broken
    install". If you would rather use the same command as everyone else, open
    **Git Bash** - Windows key, type `Git Bash`, Enter - and run the plain command
    above at the `$` prompt.

!!! note "On Windows, Ava runs in a Linux container. That is normal."
    Docker Desktop runs Linux containers inside **WSL2**, a lightweight Linux VM
    that Windows provides. So the stack on your laptop is Windows → WSL2 → a
    container → Ava. Nothing about that is a misconfiguration, but it has two
    consequences worth knowing before you meet them in Setup:

    - **Memory.** Windows gives the WSL2 VM roughly half the machine's RAM by
      default, so a 32 GB laptop reports about 15.4 GB. That is a ceiling on what
      Ava can use, not memory Windows loses. Raise it in the **WSL Settings** app
      (Start -> type "WSL Settings"), or by putting `memory=24GB` under a
      `[wsl2]` header in `%USERPROFILE%\.wslconfig`. Either way, run
      `wsl --shutdown` afterwards and start Docker Desktop again.
    - **Your graphics card.** A container is not handed a GPU unless one is
      reserved for it, and the compose file reserves one only for the inference
      service. So Setup sizes its recommendation from memory rather than from
      your card. The card still does the work; Ava's own process just cannot read
      it. See "Surfacing the GPU in the bridge container" in the
      [install reference](../docs/INSTALL_REFERENCE.md) to change that.

    Setup → Hardware says which of these apply to your machine and carries both
    fixes, so there is nothing to work out in advance.

!!! note "On a Mac (Apple Silicon)? Skip Docker."
    Docker Desktop on macOS cannot pass the Apple GPU through, so inference in a
    container runs CPU-only. Use the bare-metal path below instead (collapsed under
    "Install bare metal instead") - it reaches the Metal GPU.

## 2. The profile it picks for you

A **profile** is which set of containers Ava starts. Detection handles this: the
installer reads your hardware, chooses a row below, and prints what it chose and
why. You only pick one yourself if you want something other than what it found.

| Profile | Pick this if | Containers | What that buys |
|---|---|---|---|
| `cpu` | no GPU, under 4 GB of VRAM, or Docker cannot reach the card | ava, ollama | chat on a local model, no GPU |
| `cuda` | you have an NVIDIA card with 4-12 GB of VRAM | ava, ollama-cuda | chat on a local model, on your NVIDIA GPU |
| `gpu` | you have an NVIDIA card with 12 GB or more | ava, vllm | chat on a local model, NVIDIA |
| `rocm` | you have an AMD card or APU | ava, ollama-rocm | chat on a local model, AMD |
| `cloud` | you would rather pay an API than run a model | ava | chat against an API key you supply |
| `agent` | you want the tool-using agent (read the warning) | ava, agent, vllm | **+ the tool-using agent** that drives your connected apps |

To pin one instead: `AVA_PROFILE=agent ./install.sh`.

??? note "Why there are two NVIDIA profiles, and how to force the one you want"

    They run different **engines**, and the engines need different amounts of VRAM
    for the same model, because they load it in different formats.

    `gpu` runs **vLLM** on FP16 weights: the shipped 7B is 14.2 GiB of weights plus a
    32k KV cache at 0.90 memory utilization, so it wants ~18 GB - 12 GB with the 3B
    the installer downshifts to. `cuda` runs **Ollama** on quantized GGUF weights:
    llama3.2 (3B) at Q4_K_M is 2 GiB, and with its KV cache, CUDA context and a
    little headroom it needs about 4 GB. Everything between those two floors is a
    card that vLLM cannot use and Ollama can - a 6 GB laptop RTX, for instance.
    Before `cuda` existed those cards were sent to `cpu`, several times slower for
    exactly the same answer, because one FP16-shaped threshold decided both.

    `cuda` needs the **NVIDIA Container Toolkit**, not just the driver: `nvidia-smi`
    working proves the *host* can talk to the card, not that the daemon can hand it
    to a container. Confirm with:

    ```bash
    docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
    ```

    The installer asks the same question before it chooses, and falls back to `cpu`
    rather than starting a service Docker will refuse. **To force `cuda`** - a card
    the probe misjudged, or a runtime registered in a way `docker info` does not
    show:

    ```bash
    AVA_PROFILE=cuda AVA_SKIP_GPU_RUNTIME_CHECK=1 ./install.sh
    # or, without the installer:
    cd deploy && cp profiles/cuda.env .env && docker compose up -d
    ```

!!! warning "`agent` grants root-equivalent access to your machine"
    It starts an extra `agent` container that mounts the host Docker socket
    (`/var/run/docker.sock`), which is **root-equivalent** on the host. That is how
    it spawns the sandbox - the isolated container that runs model-generated code.
    Ava makes it opt-in because it is your call to make rather than the
    installer's. The five auto-detected profiles (`cpu`, `cuda`, `gpu`, `rocm`,
    `cloud`) never grant it.

## 3. Open the link the installer prints

The installer finishes by printing a link, and opens it in your browser for you
unless you are over SSH, in CI, or headless:

```
http://localhost:8096/setup?claim=NElSIgxdL1h4Qd6x7FbDdg
```

**Open that whole link, `?claim=` and all.** Browsing to `http://localhost:8096`
on its own shows a red "this Ava has not been claimed yet" notice, even though
you are sitting at the machine. The token proves you can read a file on the
server's disk, which is the same thing as proving the machine is yours. It is
single-use: it stops working the moment a password is set.

**If you lost the link**, ask Ava for it:

| How you installed | Command |
|---|---|
| Docker | `cd deploy && docker compose exec ava ./bin/ava claim` |
| Bare metal | `./bin/ava claim` |

Or pin `AVA_PASSWORD=...` in `deploy/.env` before the first start and skip the
gate entirely.

??? note "Why setup asks for a claim token, and the attack it stops"
    **What the token is.** A random value, unique to your install, generated the
    first time Ava needs one and written to `$AVA_HOME/data/setup_claim` (mode
    `0600`, readable only by the account Ava runs as). It is not tied to a person or
    an account - it belongs to the *instance*. It is deleted the moment an admin
    password is set, so it works until you finish setup and never again.

    **Why it exists.** The setup page has to be reachable without a password, since
    its whole job is creating the first one. Unguarded, that means the first device
    on your network to find the port sets your admin password and locks you out of
    your own install. So Ava asks for proof that you can read a file on the server's
    disk, which is the same thing as proving the machine is yours. Refusing remote
    setup outright would be the other option, but a headless box with no local
    browser has to stay claimable. Jupyter's token and Pi-hole v6's first-run
    password solve it the same way.

    **Why you see it on your own machine.** Under Docker, your request reaches Ava
    through the compose bridge network, so the container sees the gateway address
    (something like `172.18.0.1`) rather than `127.0.0.1`. It cannot tell you apart
    from anyone else on the network, so it asks everyone. A bare-metal install on
    loopback skips the gate entirely.

    That last point is also why the gate is not theatre on a stock install, even
    though `docker-compose.yml` publishes every port on `127.0.0.1` only: the compose
    file declares no `networks:` key, so `ollama`, `vllm` and the agent sandbox all
    sit on one flat bridge with an unauthenticated route to
    `http://ava:8096/setup` that never touches the published port. On the `agent`
    profile one of those neighbours is a sandbox that runs model-generated code,
    started by the same `docker compose up -d`, before any password exists.

    **Reading the token by hand.** `./bin/ava claim` is the supported route, but the
    file is there if you want it:

    ```bash
    docker compose exec ava cat /data/data/setup_claim
    # then open  http://localhost:8096/setup?claim=<token>
    ```

    On Windows in Git Bash, prefix that with `MSYS_NO_PATHCONV=1` - otherwise MSYS
    rewrites the container path into a Windows one and the command prints nothing.

    If `docker compose exec` is unavailable, the bridge printed the link on startup,
    so the logs carry it: `docker compose logs ava | grep 'setup?claim='`.

## 4. Good to know

- **All state lives in one folder** (`AVA_HOME`, default `deploy/ava-data/`):
  config, chats, media, logs, models. To back Ava up, copy that folder.

- **Setup → Hardware reads "no graphics card readable from here"** on a machine
  that plainly has one: compose grants the GPU to the *inference* service only.
  The panel says so rather than implying a driver fault, and sizes your model
  tier from system memory instead. Your card still does the work. The override is
  one file - see the [install reference](../docs/INSTALL_REFERENCE.md).

- **Your engine can live on the machine, not in the compose stack.** When Ava
  runs in a container it also probes the host (`host.docker.internal`), so a
  natively-installed Ollama or LM Studio is a supported setup - and on Windows
  or a Mac usually the faster one, since it gets the real GPU and the full
  memory. It has to listen on more than `127.0.0.1` first; Setup says so when it
  cannot reach one. Details in the
  [install reference](../docs/INSTALL_REFERENCE.md).

- **Web search does not work out of the box in Docker.** No profile starts
  SearXNG or Tor, so the switch reports the service as down rather than
  pretending. Wiring one up is in the
  [install reference](../docs/INSTALL_REFERENCE.md), with voice and the
  model-sizing knobs.

!!! note "The default install has no tools, no memory and no connectors"
    The container runs the tool-less assistant by default: chat works, but there is
    no tool use, no memory recall, no connectors and no self-editing. To add the
    **full tool-using agent** to an install you already have, switch to the `agent`
    profile: `cp profiles/agent.env .env && docker compose up -d` (that file already
    sets the `AVA_AGENT_ENABLED` / `AVA_AGENT_RUNTIME` / `AVA_ROUTER_HOST` trio,
    which all three have to be right together). It grants the same root-equivalent
    Docker socket warned about in step 2. Full setup:
    [Set up the agent](../docs/AGENT_RUNTIME.md).

??? note "Verify the signed image (optional)"
    Published images are **cosign-signed** (Sigstore keyless; the signature proves
    the image came from this repo's release CI). Pull the signed image instead of
    building locally by setting `AVA_IMAGE` in `deploy/.env`, and verify it first:

    Replace `X.Y.Z` below with a version that exists - see the
    [Releases page](https://github.com/JayMTea/Ava/releases). (`release.yml`
    publishes an image only on a `v*` tag push, so a version that has not been
    released yet returns 403 from GHCR.)

    The two spellings are not interchangeable: the **image tag is `X.Y.Z`** while the
    **git tag in the certificate identity is `vX.Y.Z`**, and the registry path is
    lowercased while the certificate identity keeps the repo's case. Substituting one
    value for both is why a copy-pasted verify fails.

    ```bash
    # Verify the release image (see SECURITY.md §9 for the exact identity regex):
    cosign verify ghcr.io/jaymtea/ava-bridge:X.Y.Z \
      --certificate-identity-regexp "https://github.com/JayMTea/.+/.github/workflows/release.yml@refs/tags/vX.Y.Z" \
      --certificate-oidc-issuer https://token.actions.githubusercontent.com

    echo "AVA_IMAGE=ghcr.io/jaymtea/ava-bridge:X.Y.Z" >> deploy/.env
    docker compose pull && docker compose up -d
    ```

    Verifying a fork's own build? Swap `jaymtea` / `JayMTea` for your owner in both
    places, keeping the same lowercase-registry, cased-identity split.

    `ollama` and `vllm` are upstream images. The `ava/bridge` image is built locally
    by default, or set `AVA_IMAGE` to the signed published image (above). Model
    weights download on first run and carry their own licenses (surfaced at setup).

??? note "Install bare metal instead (Python, your own GPU stack, or a Mac)"
    If you would rather run it directly - for example, you already have Python and a
    GPU stack, or you are on Apple Silicon where Docker cannot reach the GPU:

    ```bash
    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    cd frontend && npm install && npm run build && cd ..

    ./bin/ava setup              # creates AVA_HOME, generates secrets + admin password, ava.yaml
    ./bin/ava models pull --auto # downloads a model that fits your hardware (once, large)

    # Start an inference engine - `ava up` runs the WEB APP, never an engine.
    bash deploy/local-serve.sh   # NVIDIA + Docker: serves the model with vLLM
    # Apple Silicon / CPU:  ollama serve  &&  ollama pull <tag>  (see CHOOSE_A_MODEL.md)

    ./bin/ava doctor     # verifies hardware, dirs, config, inference, services
    ./bin/ava up         # runs the web app on http://localhost:8096
    ```

    The engine step is the one people skip, and skipping it produces a working web
    app whose first message fails - so `ava doctor` **exits non-zero** when nothing
    can serve a chat turn, which stops the `&&` chain right at the missing step.

    `ava setup` prints your generated admin password (or pass `--password`).

    `./bin/ava` works from the checkout with no install step. To get a plain `ava`
    command on your `PATH` instead, swap the `pip install -r requirements.txt` line
    for `pip install -e .` - it installs the same dependencies and adds the console
    script. Keep the `-e`: Ava runs *from* this checkout, and a non-editable install
    would leave it looking for `frontend/dist`, `config.example.yaml` and
    `agent/install.sh` inside `site-packages`, where they are not.

    A healthy run looks like this - `doctor` shows the hardware it detected, and
    `up` prints the address to open:

    ![Terminal: ava setup and ava doctor passing with green checks, including hardware Apple M4 Max with 128 GB unified memory, then ava up printing http://localhost:8096](../docs/assets/install-1-terminal.png)

    Engine wiring, per-engine tool-calling support and the Apple Silicon recipe are
    in the [install reference](../docs/INSTALL_REFERENCE.md).

## 5. Check it worked

Open **Setup → Hardware**. Ava should show your machine - chip, usable memory,
and a recommended model tier - detected automatically, with nothing for you to
configure.

Under Docker the GPU row reads as unavailable and the tier is sized from system
memory instead, as described above. That is expected, not a fault.

## Troubleshooting

- Health check: `curl http://localhost:8096/api/health`.
- Docker logs: `docker compose logs -f ava`.
- Bare metal: `ava doctor` is the first stop; it shows what is missing.
- No GPU? `cd deploy && cp profiles/cpu.env .env` (Ollama) or
  `cp profiles/cloud.env .env` (an API key), then `docker compose up -d`. The
  profile lives in `.env`, never as `--profile` on the command line.
- Everything else - manual profiles, the env-var reference, engine wiring,
  Apple Silicon, `ava.yaml` - is in the
  [install reference](../docs/INSTALL_REFERENCE.md).

---

## Next: pick a model

Ava has no brain until you point it at one. That happens in the app and takes
about a minute.

**→ [Step 2: Pick a model](../docs/CHOOSE_A_MODEL.md)**
