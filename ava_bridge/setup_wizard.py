"""First-run web wizard — extends the existing /setup surface.

Server-rendered so it works before the SPA bundle exists / when it's stale, and
so onboarding never couples to the frontend build. Step 1 (the password screen
in phone_bridge) stays the only PRE-auth surface; everything here is behind the
session cookie (these routes are not in auth._PUBLIC_PATHS, so auth_gate covers
them automatically — no middleware change).

Everything the wizard sets is written to $AVA_HOME/ava.yaml via
settings.save_patch — no source edits, ever.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import brand as _brand, config, connectors, features, settings

router = APIRouter()


def _private(path: str, flags: int) -> int:
    """open() opener that creates the file 0600, so an API key never exists
    world-readable in the window between create and chmod."""
    return os.open(path, flags, 0o600)

with open(os.path.join(config.WEB_DIR, "setup_wizard.html"), encoding="utf-8") as _f:
    _WIZARD_PAGE = _f.read()


def setup_completed() -> bool:
    """True once onboarding is done — the flag, and nothing else.

    This used to also return True whenever `inference.backends` was non-empty,
    to avoid dropping an upgraded install back into the wizard. But `ava setup`
    copied config.example.yaml, which ships a live `inference.backends.local` —
    so the flag was already effectively True the moment setup finished, and every
    CLI-installed user had the first-run wizard silently skipped without ever
    seeing it.

    The heuristic's goal is met structurally instead: the wizard is re-entrant
    (see wizard_page), so landing on it is a two-click annoyance rather than a
    trap, and `ava setup` now writes a minimal ava.yaml with no backend at all.
    """
    return settings.get_bool("setup.completed", False)


@router.get("/setup/wizard", response_class=HTMLResponse)
def wizard_page():
    """Always reachable.

    It used to 303 away once complete, which combined badly with `api_save`
    marking completion before validating: a blank model field returned
    {"ok": true}, set the flag, wrote no backend — and locked the only screen
    that could fix it. Re-entrant means the worst case is landing here again.
    """
    return HTMLResponse(_brand.render_page(_WIZARD_PAGE))


@router.get("/api/setup/hardware")
def api_hardware():
    from . import hwinfo, model_fit
    # `note_code` names the SITUATION; the wording lives in whichever surface is
    # rendering it (CLAUDE.md: backend returns facts, owner-facing copy lives in
    # the frontend). This used to ship the sentence itself, which meant the
    # wizard and Setup → Hardware had to show the same paragraph even though one
    # is a first-run screen with no room and the other is the page someone opens
    # precisely because they want the detail. Codes:
    #   apple-silicon    — unified memory; vLLM cannot serve here
    #   container-no-gpu — a container that was not granted a GPU
    out = {"fit_gb": None, "free_gb": None, "source": None, "tier": "cloud",
           "hint": "", "gpu": None, "platform": None, "note_code": "",
           # WHICH pool the number above is. Without these the page could only
           # print a size next to an accelerator's name and hope they matched —
           # which on a 6 GB laptop card inside Docker rendered the card's name
           # beside the host's 15.4 GB of RAM.
           "pool_kind": "unknown", "accelerated": False,
           "accel_measurable": False, "capped": False, "cap_kind": None,
           # The operator-stated pool: whether one is in force, what the box
           # actually measured, where the value came from (so the UI can say
           # "an environment variable set this, not this form"), and whether
           # the two disagree in the direction that OOM-kills.
           "stated": False, "measured_gb": None, "stated_source": "",
           "overstated": False}
    try:
        out["platform"] = hwinfo.platform_id()
    except Exception:  # noqa: BLE001
        pass
    try:
        pool = hwinfo.fit_pool()
        out["fit_gb"] = round(pool.total_gb, 1) if pool.total_gb else None
        out["free_gb"] = round(pool.free_gb, 1) if pool.free_gb else None
        out["source"] = pool.source
        out["pool_kind"] = pool.kind
        out["accelerated"] = pool.accelerated
        out["accel_measurable"] = pool.accel_status == "measured"
        out["capped"], out["cap_kind"] = pool.capped, pool.cap_kind
        out["stated"] = pool.stated
        out["measured_gb"] = round(pool.measured_gb, 1) if pool.measured_gb else None
        if pool.stated:
            # WHERE it came from decides whether the Setup form can change it.
            # An env var beats ava.yaml by design, so a form that silently wrote
            # a value the process would keep ignoring is worse than no form.
            out["stated_source"] = ("env" if os.environ.get("AVA_FIT_MEM_GB")
                                    else "config")
            # Stating MORE than the box can actually give is the one way this
            # override hurts: the tier goes up, the model does not fit, and the
            # kernel kills it on first load. Understating is merely cautious, so
            # only the dangerous direction is flagged.
            out["overstated"] = bool(
                pool.measured_gb and pool.total_gb
                and pool.total_gb > pool.measured_gb * 1.05)
        if pool.total_gb:
            tier, hint = model_fit.recommend_tier(pool.total_gb)
            out["tier"], out["hint"] = tier, hint
        else:
            out["hint"] = "no local GPU/unified pool detected — use a cloud key"
    except Exception as e:  # noqa: BLE001
        out["hint"] = f"probe failed: {e}"
    # Apple Silicon: unified memory is the pool; GPU util/temp/power have no
    # unprivileged API (blank meters are expected), and vLLM can't serve here —
    # use Ollama / MLX / LM Studio. Surface this so a Mac first-run isn't puzzling.
    if out["platform"] == "darwin-apple":
        out["note_code"] = "apple-silicon"
    try:
        from . import hardware
        g = hardware._gpu()
        if g.get("name"):
            out["gpu"] = g["name"]
    except Exception:  # noqa: BLE001
        pass
    # A container sees no GPU unless it was granted one, and the compose file
    # grants it only to the inference service. Saying "No local GPU detected" on
    # a box that plainly has one reads as a broken install and sends people
    # hunting for a driver problem they do not have — so name the actual reason
    # and the actual fix.
    #
    # Gated on UNMEASURABILITY, not on namelessness. `not out["gpu"]` looked
    # equivalent and is not: under WSL2 the DRM card has no readable PCI vendor,
    # so the name degrades to the literal string "GPU" (hwinfo._vendor_label),
    # which is truthy — and this explanation was therefore suppressed on exactly
    # the machines that needed it. deploy/README.md promises "the panel says so
    # rather than implying a driver fault"; this is what keeps that true.
    if not out["accel_measurable"] and not out["note_code"]:
        try:
            from .auth import in_container
            if in_container():
                out["note_code"] = "container-no-gpu"
        except Exception:  # noqa: BLE001
            pass
    return out


def _probe(url: str, timeout: float = 1.5, strict: bool = False) -> bool:
    """Is something serving here?

    `strict` is for BLIND candidates — the ports Ava guesses at rather than ones
    the owner named. The lenient rule (`< 500`) is right for a configured
    backend, where a 401 means "there, and asking for a key" rather than "gone".
    It is wrong for a port sweep: a 404 from an unrelated dev server on :8080
    also passes it, and would be reported to the user as llama.cpp answering.
    Since the sweep now covers every port in the registry on both this container
    and its host, that stopped being hypothetical.
    """
    try:
        import requests
        r = requests.get(url, timeout=timeout)
        if not strict:
            return r.status_code < 500
        if not (200 <= r.status_code < 300):
            return False
        # Every engine's health surface answers JSON. llama.cpp's /health is the
        # narrowest of them and still returns `{"status": "ok"}`.
        return "json" in r.headers.get("content-type", "").lower()
    except Exception:  # noqa: BLE001
        return False


# How to ask an engine "are you there" — derived from the engine registry so a
# newly supported engine cannot be probeable-in-theory and unprobeable-in-fact.
# It previously listed three engines while the UI offered six presets, so picking
# MLX or llama.cpp got you a base URL the wizard could not then health-check.
from . import engines as _engines  # noqa: E402 — local import, cycle-free

_HEALTH_PATH = _engines.health_paths()


_DEFAULT_PORTS = _engines.default_ports()


def _engine_of(base_url: str, declared: str = "") -> str:
    """Guess the engine family from a base URL.

    Defaulting to vLLM was wrong on a Mac: `mlx_lm.server` and `llama-server`
    both default to :8080, and calling either "vllm" means the wizard health-checks
    the wrong path AND `engine_servable_here` refuses it on darwin-apple. Ports
    are the only signal available here, so ambiguous ones resolve to the engine
    that is actually servable rather than to the NVIDIA one.

    The port ladder used to be written out here, a second time, while
    `_candidates()` hardcoded its own copy of the same numbers. Both now read
    `engines.default_ports()`, so an engine added to the registry becomes
    recognisable AND discoverable in one edit instead of neither.
    """
    if declared:
        return declared
    from urllib.parse import urlparse
    u = (base_url or "").lower()
    # Name in the URL beats port: a compose service is `http://ollama:11434/v1`,
    # but someone may equally run llama.cpp on Ollama's own port.
    for key in ("ollama", "lmstudio", "lm-studio", "vllm", "mlx",
                "llamacpp", "llama.cpp", "llama-server"):
        if key in u:
            found = _engines.get(key)
            return found.key if found else key
    try:
        parsed = urlparse(u)
        port, host = parsed.port, (parsed.hostname or "")
    except Exception:  # noqa: BLE001 — unparsable: fall back to the old default
        port, host = None, ""
    if port and port in _DEFAULT_PORTS:
        return _DEFAULT_PORTS[port]
    # A remote host is a cloud provider, not an unlabelled local vLLM. Falling
    # through to "vllm" meant `https://api.openai.com/v1` was called vLLM, and
    # `engine_servable_here("vllm")` then refuses on Apple Silicon and CPU-only —
    # so adding a perfectly good CLOUD backend was rejected for needing a GPU.
    if host and host not in _LOCAL_HOSTNAMES and not host.startswith("192.168.") \
            and not host.startswith("10.") and "." in host:
        return "openai"
    return "vllm"


# Hostnames that mean "this box or this compose network", so anything else is
# remote. Compose service names have no dot, which is why the check above also
# requires one — `http://vllm:8002/v1` must stay vLLM.
_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1", "0.0.0.0",
                    "host.docker.internal", "host.openshell.internal",
                    "host.containers.internal"}

# The names a container runtime gives the MACHINE it is running on. Docker
# Desktop publishes the first on Windows and macOS; Podman publishes the second;
# a Linux compose file opts in with `host-gateway`.
#
# Why this matters more than it looks: Ava's own wizard tells a user with no
# engine to "install Ollama (ollama.com)", and on Windows that installs a NATIVE
# Ollama — which gets the real GPU and the machine's full memory, neither of
# which the bridge container has. Ava then failed to find it, because the only
# loopback it knew was its own. The most likely path a Windows tester takes
# ended at "nothing answering yet" on a machine where something was answering.
_HOST_GATEWAYS = ("host.docker.internal", "host.containers.internal",
                  "host.openshell.internal")


def host_gateway() -> str | None:
    """The name this container can reach its host by, or None.

    Resolution-gated on purpose: an unresolvable name costs a DNS timeout PER
    PROBE, and the setup screen blocks on this. Asking the resolver once is
    cheap and fails fast, so a bare-metal install pays nothing for a code path
    that cannot apply to it.
    """
    import socket
    for name in _HOST_GATEWAYS:
        try:
            socket.getaddrinfo(name, None)
            return name
        except OSError:
            continue
    return None


def host_reachability(gw: str, timeout: float = 1.0) -> str:
    """WHY a container cannot reach an engine on its host. "" if it can.

    Two very different causes look identical in the UI, and the fix for one is
    useless against the other:

      refused  the machine answered with a reset - it is reachable, and nothing
               is listening on an address this container can use. That is the
               bind-address case: Ollama's installer binds 127.0.0.1 only.
      dropped  packets went nowhere. Something is filtering between the container
               and the machine. On Windows that is usually Defender Firewall
               blocking the WSL vEthernet adapter, and no amount of OLLAMA_HOST
               will help.

    Found by running the real thing: a container on the maintainer's box, with a
    live engine on the host and a resolvable gateway, still timed out - and the
    advice this endpoint would have given ("set OLLAMA_HOST=0.0.0.0") was the
    wrong advice for the failure that actually occurred.

    A raw connect rather than an HTTP request, because the distinction lives at
    the TCP layer: `requests` flattens both into ConnectionError subclasses whose
    text differs by wording alone.
    """
    import socket
    from concurrent.futures import ThreadPoolExecutor

    def _try(port: int) -> str:
        s = socket.socket()
        s.settimeout(timeout)
        try:
            s.connect((gw, port))
            return "open"                  # something is listening and reachable
        except (socket.timeout, TimeoutError):
            return "dropped"
        except ConnectionRefusedError:
            return "refused"               # reachable, nothing on this port
        except OSError:
            return "dropped"               # no route / host unreachable
        finally:
            s.close()

    # Concurrent, and one attempt per distinct PORT rather than per engine —
    # llama.cpp and MLX share 8080, so the naive loop dialled it twice. Measured
    # against a genuinely filtered host: sequential took 6.5 s, which is far too
    # long to hold the first screen of an install. The whole point of this probe
    # is that the timeout IS the signal, so it cannot be shortened away.
    ports = sorted({e.default_port for e in _engines.probeable_local()})
    with ThreadPoolExecutor(max_workers=len(ports)) as pool:
        seen = set(pool.map(_try, ports))
    if "open" in seen:
        return ""
    return "dropped" if "dropped" in seen else "refused"


def _locality(base_url: str) -> str:
    """WHERE an engine is, relative to Ava — not merely whether it answered.

    The distinction the fit layer needs: a `host` engine runs on the machine
    itself, outside this container, so the pool Ava measured is NOT the pool
    that engine draws on and no tier may be recommended from it.
    """
    from urllib.parse import urlparse
    host = (urlparse(base_url or "").hostname or "").lower()
    if not host:
        return "unknown"
    if host in _HOST_GATEWAYS:
        return "host"
    if host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
        # Inside a container this is the CONTAINER's loopback, which is a
        # different machine from the user's; on bare metal it is the box itself.
        from .auth import in_container
        return "container" if in_container() else "host"
    if "." not in host:
        return "compose"          # a service name on the compose network
    return "remote"


def _health_url(base_url: str, engine: str) -> str:
    """Where to ask THIS engine whether it is there.

    Reads the registry rather than hardcoding a ladder. `_HEALTH_PATH` was
    already being built from `engines.health_paths()` and then never consulted,
    so llama.cpp declared `/health` and was probed at `/v1/models` anyway — the
    precise "probeable in theory, unprobeable in fact" drift the registry exists
    to prevent, sitting one line under the comment saying so.
    """
    base = (base_url or "").rstrip("/")
    e = _engines.get(engine)
    path = _HEALTH_PATH.get(engine) or (e.health_path if e else "/models")
    # A native path hangs off the server root; the OpenAI `/models` hangs off the
    # `/v1` base. Probing Ollama at `…/v1/api/tags` reported a healthy engine down.
    if e is not None and e.health_at_root and base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base + path


def _candidates() -> list[dict]:
    """Where an engine might actually be, most-authoritative first.

    The probe used to hardcode `127.0.0.1:8002` and `127.0.0.1:11434`. Inside the
    Docker image that is the CONTAINER's loopback, and the compose engines live at
    `vllm:8002` / `ollama:11434` on the compose network — so every Docker first-run
    reported "no local engine yet" and the wizard preselected cloud, for a user who
    had just started a local engine on purpose.
    """
    seen: set[str] = set()
    out: list[dict] = []

    def add(cid: str, base: str, engine: str = "", note: str = "",
            model: str = "", blind: bool = True):
        base = (base or "").strip().rstrip("/")
        if not base or base in seen:
            return
        seen.add(base)
        key = _engine_of(base, engine)
        # The engine's PROPER name, carried with the fact. A frontend title-casing
        # the key renders "Vllm" and "Lmstudio", and a second name table per
        # frontend is exactly the drift the registry exists to prevent. This is
        # the sanctioned exception in CLAUDE.md: registry labels travel with the
        # data because they have to be self-contained.
        found = _engines.get(key)
        out.append({"id": cid, "base_url": base,
                    "engine": key,
                    "engine_label": found.label if found else key,
                    "note": note,
                    "locality": _locality(base),
                    # Guessed at by Ava, or named by the owner? Only the former
                    # gets the strict probe — see _probe.
                    "blind": blind,
                    # The MODEL each source names. This was read and thrown away:
                    # both sources below already carry one, and dropping it is why
                    # the wizard demanded the user retype a value it was holding.
                    "model": (model or "").strip()})

    # 1. What this install already declares.
    for bid, spec in (settings.get("inference.backends", {}) or {}).items():
        if isinstance(spec, dict):
            add(bid, spec.get("base_url", ""), spec.get("engine", ""),
                "configured", spec.get("model", ""), blind=False)
    # 2. What the environment points at (compose sets this per profile).
    add("env", os.environ.get("AVA_BACKEND_URL", ""),
        os.environ.get("AVA_BACKEND_ENGINE", ""), "from AVA_BACKEND_URL",
        os.environ.get("AVA_BACKEND_MODEL", ""), blind=False)
    # 3. The compose service names, for a container that has neither.
    add("vllm-service", "http://vllm:8002/v1", "vllm", "compose service")
    add("ollama-service", "http://ollama:11434/v1", "ollama", "compose service")
    # 4. Bare metal — this box's own loopback, which inside a container is the
    #    CONTAINER's loopback and finds only what compose started.
    for e in _engines.probeable_local():
        add(f"{e.key}-local", f"http://127.0.0.1:{e.default_port}/v1", e.key, "local")
    # 5. The machine this container runs ON. Last, because a compose engine that
    #    Ava started is a better answer than one it merely found — but present,
    #    because on Windows and macOS a natively-installed engine is the common
    #    case and was previously invisible. Costs one DNS lookup on bare metal.
    gw = host_gateway()
    if gw:
        for e in _engines.probeable_local():
            add(f"{e.key}-host", f"http://{gw}:{e.default_port}/v1", e.key,
                "on the host machine")
    return out


def _pull_cmd(base_url: str, engine: str, model: str) -> str:
    """How to fetch a declared-but-absent model, for the box it is missing on.

    The compose base URLs ARE the service names (`ollama`, `ollama-cuda`,
    `ollama-rocm`, `vllm`), so this is derived rather than guessed — and getting
    it wrong is expensive here: `docker compose exec ollama ...` fails outright
    under the rocm profile, where the service is `ollama-rocm`, and under cuda,
    where it is `ollama-cuda`.

    vLLM gets nothing: it loads one model at boot from --model, so a missing one
    is a restart with different flags, not a pull.
    """
    if engine != "ollama" or not model:
        return ""
    try:
        from urllib.parse import urlparse
        host = (urlparse(base_url).hostname or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    if not host:
        return ""
    if host in _LOCAL_HOSTNAMES:
        return f"ollama pull {model}"
    return f"docker compose exec {host} ollama pull {model}"


def recommend_brain() -> dict:
    """What is already going to do the thinking, and what that costs the owner.

    First run asked the user to TYPE a model id, and refused to finish without
    one — while the answer sat in the bridge's own environment. `_candidates()`
    read AVA_BACKEND_URL and AVA_BACKEND_ENGINE and stopped one variable short of
    AVA_BACKEND_MODEL, which docker-compose.yml sets from the AVA_MODEL that
    install.sh already resolved and pulled. A novice cannot know that string, and
    a typo produces a config that looks right and fails at their first message.

    `writes_config` is the load-bearing field. router_app.load_backends() builds
    the env backend ONLY when inference.backends is absent or empty, so writing
    anything there — including the correct value — permanently shadows the
    installer's backend and stops it tracking deploy/.env. When the source is
    already authoritative there is nothing to save, and saying so is the whole
    point: the wizard should confirm, not copy.

    Deliberately does NOT ask the engine what it holds. That needs a models
    endpoint per engine and belongs in its own change; this reads config and
    environment, plus one cheap health probe so the card can say whether the
    thing is answering yet.
    """
    out = {"source": "none", "model": "", "engine": "", "base_url": "",
           "live": False, "writes_config": True, "consequence": "", "tier": "",
           "served": [], "pull_cmd": "", "agent_owns_it": False}

    # BEFORE everything below: when the agent runtime is live it OWNS the model
    # endpoint, and turns never touch the inference backends this function reads.
    # Without this the wizard confirmed a backend that would not serve a single
    # message — the owner picks a model, the box uses a different one, and the
    # two screens that report the brain disagree forever.
    #
    # Asked through the ONE resolver rather than by re-reading `sandbox_info`
    # here, which would make this a fourth independent answer to "which model is
    # live" (agent.which_model and BrainPanel were the other two).
    try:
        from . import models as _models
        brain = _models.effective_brain()
    except Exception:  # noqa: BLE001 — never let this break the wizard
        brain = {}
    if brain.get("source") == "agent":
        model = str(brain.get("model_id") or "")
        return {**out, "source": "agent", "agent_owns_it": True,
                "model": model,
                "engine": str(brain.get("engine") or ""),
                # Nothing here is the wizard's to write: the sandbox holds the
                # endpoint, and `nemoclaw onboard` is what set it.
                "writes_config": False,
                # An empty model is a real and common state — the sandbox exists
                # but its container is not running, so nothing could read what it
                # holds. Saying so beats naming a backend that will not serve a
                # single turn, and beats claiming a model we did not read.
                "live": bool(model),
                "consequence": (
                    "Your agent sandbox already holds the model it thinks with, "
                    "so this step has nothing to change. Change it with "
                    "`nemoclaw onboard`." if model else
                    "Your agent runtime owns the model, but it is not running, so "
                    "Ava cannot read which one. Start it and check Setup → Agent; "
                    "anything chosen here would not be used.")}

    # Priority: what the owner (or a previous run) CHOSE beats what the installer
    # arranged, because a chosen backend is already in force and already written.
    primary = settings.get("inference.primary") or ""
    backends = settings.get("inference.backends", {}) or {}
    spec = None
    if isinstance(backends, dict) and backends:
        if primary and isinstance(backends.get(primary), dict):
            spec = backends[primary]
        else:
            for v in backends.values():
                if isinstance(v, dict) and str(v.get("model", "")).strip():
                    spec = v
                    break
    if isinstance(spec, dict) and str(spec.get("model", "")).strip():
        out.update(source="configured", model=str(spec["model"]).strip(),
                   base_url=str(spec.get("base_url", "")).strip(),
                   engine=str(spec.get("engine", "")).strip(),
                   # Already in ava.yaml — confirming changes nothing.
                   writes_config=False)
    else:
        url = os.environ.get("AVA_BACKEND_URL", "").strip()
        model = os.environ.get("AVA_BACKEND_MODEL", "").strip()
        if url and model:
            out.update(source="installed", model=model, base_url=url.rstrip("/"),
                       engine=os.environ.get("AVA_BACKEND_ENGINE", "").strip(),
                       # THE point of this whole endpoint: the installer's value
                       # is live via the env backend, so confirming it must not
                       # copy it into ava.yaml and freeze it.
                       writes_config=False)

    if out["base_url"]:
        out["engine"] = _engine_of(out["base_url"], out["engine"])
        out["live"] = _probe(_health_url(out["base_url"], out["engine"]))

    # Ask the engine what it is actually holding, and adopt ITS spelling. Ava
    # sends the id verbatim, so "close enough" is not a category: a pulled
    # `llama3.2` is reported by Ollama as `llama3.2:latest`, and vLLM is
    # case-sensitive about `mistralai/Mistral-7B-Instruct-v0.3`.
    if out["live"] and out["base_url"]:
        from . import models as _models
        out["served"] = _models.served_models(out["base_url"], out["engine"])
        if out["model"] and out["served"]:
            exact = _models.match_served(out["model"], out["served"])
            if exact:
                out["model"] = exact
            else:
                # Declared but absent. Only reachable when the list was READ —
                # an engine that is down or slow returns [] and must degrade to
                # "we do not know", never to "your model is missing".
                out["source"] = "declared_missing"
                out["pull_cmd"] = _pull_cmd(out["base_url"], out["engine"],
                                            out["model"])

    # What it costs them, in consequences rather than parameters. Reuses the tier
    # the hardware step already shows, so the two screens cannot disagree.
    try:
        from . import hwinfo, model_fit
        mem = hwinfo.fit_memory()
        if mem.total_gb:
            tier, _hint = model_fit.recommend_tier(mem.total_gb)
            out["tier"] = tier
    except Exception:  # noqa: BLE001 — a missing tier costs a sentence, not the step
        pass
    if out["model"]:
        if out["tier"] in ("tiny", "cloud", ""):
            out["consequence"] = (
                "It runs on your processor rather than a graphics card, so "
                "answers arrive a few words at a time and it gets facts wrong "
                "more often than a big hosted model. Good enough to see how "
                "Ava works.")
        elif out["tier"] == "small":
            out["consequence"] = (
                "This machine can run a mid-sized model, so answers should "
                "arrive at a readable pace. It will still be less knowledgeable "
                "than a large hosted model.")
        else:
            out["consequence"] = (
                "This machine has room for a large model, so quality should be "
                "close to a hosted service and nothing leaves the box.")
    return out


@router.get("/api/setup/brain")
def api_brain():
    """What will do the thinking, so first run can confirm rather than ask."""
    return recommend_brain()


@router.get("/api/setup/backends")
def api_backends():
    """Which local engines are answering right now, so the wizard can preselect
    the truth rather than a guess.

    Probed CONCURRENTLY. Sequentially this was two loopback candidates and cheap;
    sweeping the registry's ports on both this container and its host is ten, and
    at a 1.5 s timeout each a firewall that drops rather than refuses would have
    held the first setup screen for fifteen seconds. Order is restored afterwards
    so "most authoritative first" still holds — the wizard preselects
    `backends[0]`, and that must stay a decision rather than a race.
    """
    from concurrent.futures import ThreadPoolExecutor
    cands = _candidates()
    with ThreadPoolExecutor(max_workers=min(12, len(cands) + 1)) as pool:
        ups = list(pool.map(
            lambda c: _probe(_health_url(c["base_url"], c["engine"]),
                             strict=c.get("blind", True)), cands))
    found = [{**c, "up": up} for c, up in zip(cands, ups)]
    gw = host_gateway()
    return {
        "backends": found,
        "any_up": any(b["up"] for b in found),
        "router": _probe(f"http://127.0.0.1:{config.ROUTER_PORT}/healthz"),
        # Facts the "nothing answering yet" case needs, because from in here
        # "not installed" and "installed but bound to loopback" look identical.
        # An engine on the host machine is only reachable if it listens on more
        # than 127.0.0.1, and Ollama's Windows installer does not by default —
        # so the honest message names that cause instead of implying absence.
        "in_container": _in_container(),
        "host_gateway": gw or "",
        # Only asked when it is the question: nothing answered, and there IS a
        # host out there that might have. Costs one connect per engine port in
        # the failure case and nothing at all the rest of the time.
        "host_reach": (host_reachability(gw)
                       if gw and _in_container()
                       and not any(b["up"] for b in found) else ""),
    }


def _in_container() -> bool:
    try:
        from .auth import in_container
        return in_container()
    except Exception:  # noqa: BLE001 — telemetry must never break the probe
        return False


@router.get("/api/setup/features")
def api_features():
    """The optional-capability list, derived from features.REGISTRY.

    The wizard's HTML hardcoded three checkbox ids, so a newly registered
    capability needed an HTML edit to appear — while the SAVE path was already
    registry-driven. Same contract tests/test_feature_convention.py enforces
    elsewhere: one registry entry, no further wiring.
    """
    return {"features": features.snapshot()}


@router.get("/api/setup/connectors")
def api_connectors():
    out = []
    for m in connectors.load(force=True):
        out.append({"id": m["id"], "label": m.get("label", m["id"]),
                    "kind": m.get("kind", "app")})
    return {"connectors": out}


def _write_inference_key(key: str) -> None:
    """Write the cloud provider's API key to the secrets store, 0600.

    A separate named sync helper so the async route can hand it to
    run_in_threadpool — see tests/test_no_blocking_routes.py for why.
    """
    path = os.path.join(settings.secrets_dir(), "inference_key")
    os.makedirs(settings.secrets_dir(), exist_ok=True)
    with open(path, "w", encoding="utf-8", opener=_private) as f:
        f.write(key)
    os.chmod(path, 0o600)

@router.post("/api/setup/save")
async def api_save(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)

    inf = body.get("inference") or {}
    mode = str(inf.get("mode", "local"))
    # "Skip for now": save the preferences, do NOT claim onboarding is done.
    # Without it a user with no model yet had to either lie or abandon the page.
    skip = bool(body.get("skip_inference"))
    patch: dict = {}
    # Things that are true about the saved config but are not refusals. The
    # wizard used to answer a flat `{"ok": true}` for a backend it had never
    # asked a single question of.
    out_warnings: list[str] = []

    # VALIDATE FIRST. `patch = {"setup": {"completed": True}}` used to be the
    # first line of this function, and the local branch was `if base and model:`
    # with no else — so a blank model field returned {"ok": true}, marked
    # onboarding complete, and wrote no backend at all. Combined with the wizard's
    # one-shot redirect there was then no way back to the only screen that could
    # fix it. Reproduced before this change.
    if not skip:
        if mode == "installed":
            # The owner confirmed what is already in force. Re-derive it here
            # rather than trusting the body: the client asserting "there is a
            # brain" would be a way to complete setup against nothing.
            brain = recommend_brain()
            if not brain["model"]:
                return JSONResponse(
                    {"error": "Nothing is set up to do the thinking yet. Choose "
                              "a local engine or a cloud provider.",
                     "field": "model"}, status_code=400)
            if brain["source"] == "declared_missing":
                # The engine answered and does NOT hold this model. Completing
                # here would hand back a finished setup whose first message 404s,
                # which is the failure this whole screen exists to prevent.
                return JSONResponse(
                    {"error": f"{brain['engine'] or 'The engine'} is running but "
                              f"does not have {brain['model']!r} yet. Download it "
                              "first, then try again.",
                     "field": "model"}, status_code=400)
            # And write NO inference block. router_app.load_backends() builds the
            # env backend only while inference.backends is empty, so copying the
            # installer's value in would shadow it and stop it tracking
            # deploy/.env for good. Confirming is not choosing.
        elif mode == "cloud":
            base = str(inf.get("base_url", "")).strip()
            model = str(inf.get("model", "")).strip()
            if not base:
                return JSONResponse({"error": "Enter your provider's API base URL "
                                              "(it usually ends in /v1).",
                                     "field": "base_url"}, status_code=400)
            if not model:
                return JSONResponse({"error": "Enter the model id your provider "
                                              "expects, e.g. gpt-4o-mini.",
                                     "field": "model"}, status_code=400)
            patch["inference"] = {
                "primary": "cloud",
                "backends": {"cloud": {"engine": "openai", "base_url": base,
                                       "model": model,
                                       "api_key_env": "AVA_INFERENCE_KEY"}},
            }
            key = str(inf.get("api_key", "")).strip()
            if key:
                # Secret goes to the secrets store, never into ava.yaml.
                # Off the event loop: api_save is an `async def` route, so a bare
                # open()/write() here blocks every other request — including the
                # SSE streams and the login gate — for the duration of the disk
                # write. Same idiom as _store_upload in ava_bridge/media_api.py.
                await run_in_threadpool(_write_inference_key, key)
        else:
            engine = str(inf.get("engine", "")).strip() or "vllm"
            base = str(inf.get("base_url", "")).strip()
            model = str(inf.get("model", "")).strip()
            if not base:
                return JSONResponse({"error": "Enter the address your engine "
                                              "serves on (it usually ends in /v1).",
                                     "field": "base_url"}, status_code=400)
            if not model:
                return JSONResponse(
                    {"error": "Enter the model your engine is serving — Ava sends "
                              "this id verbatim, so it must match exactly.",
                     "field": "model"}, status_code=400)
            # Ask the engine before writing it down. The `installed` branch above
            # already re-derives and refuses a `declared_missing` model; this one
            # validated non-emptiness and nothing else, so a typo'd id or an
            # engine that cannot run here completed setup and failed at the first
            # message — with the wizard, the one screen that could fix it,
            # reporting success. `BrainPanel` has had a Test-connection button
            # this whole time; first run did not.
            #
            # A WARNING, not a refusal. The engine may legitimately be starting,
            # or be behind something slow, and refusing would strand an owner
            # whose setup is fine. What is not acceptable is silence.
            from . import models as _models
            warnings: list[str] = []
            if not _models.engine_servable_here(engine):
                warnings.append(
                    f"{engine} cannot serve on {_models.platform_label()}. Ava saved "
                    "it, but nothing will answer until you pick an engine this "
                    "machine can run.")
            reachable, served = await run_in_threadpool(
                _models.probe_serving, base, engine, "", 2.0)
            if not reachable:
                warnings.append(
                    f"Nothing answered at {base}. That is fine if the engine is "
                    "still starting; if it is running, check the address.")
            elif served and not _models.match_served(model, served):
                warnings.append(
                    f"{engine} is running but is not serving {model!r} — Ava sends "
                    f"that id verbatim. It has: {', '.join(served[:4])}.")

            patch["inference"] = {
                "primary": "local",
                "backends": {"local": {"engine": engine, "base_url": base,
                                       "model": model}},
            }
            out_warnings.extend(warnings)

    feats = body.get("features") or {}
    if isinstance(feats, dict):
        # Whitelist = the feature registry, so a newly registered capability's
        # toggle works here with no further wiring.
        patch["features"] = {k: bool(v) for k, v in feats.items()
                             if k in features.REGISTRY}

    enabled = body.get("connectors")
    if isinstance(enabled, list):
        patch["connectors"] = [str(c) for c in enabled]

    # ...and only now is onboarding actually done.
    if not skip:
        patch["setup"] = {"completed": True}

    try:
        settings.save_patch(patch)
    except settings.ConfigParseError as e:
        return JSONResponse({"error": str(e), "error_code": "config_unparseable"},
                            status_code=409)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"could not write ava.yaml: {e}"},
                            status_code=500)
    # config.py reads several of these at import, so a restart applies them fully.
    return {"ok": True, "restart_required": True, "completed": not skip,
            "warnings": out_warnings}
