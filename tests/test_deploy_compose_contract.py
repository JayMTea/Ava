"""A profile must start the service its own backend URL points at.

The `ava` service has no `profiles:` key, so it runs under every profile. That
made its backend settings global, and the single default they carried —
`AVA_BACKEND_URL=http://vllm:8002/v1` — was correct for exactly one profile. Under
`--profile cpu`, which deploy/install.sh selects for every GPU-less box and every
card under 16 GB, no `vllm` service exists, so the bridge spent every turn
resolving a hostname that would never exist. After a multi-GB pull, with nothing
in the UI saying why.

Nothing caught it because `docker compose config` is perfectly happy to
interpolate a default that names a service the selected profile never starts.
Comparing the two is what this file does.

The backend trio now has no defaults at all (`${VAR:?...}`), and lives in
deploy/profiles/<profile>.env instead. That only works while every profile file
actually sets every guarded variable, which is the other half of this guard.

Style matches tests/test_diagram_sync.py: a `git ls-files` scan, no bridge, no
AVA_HOME, no Docker daemon — PyYAML is already a runtime dependency, so the
compose file is parsed directly.
"""
import pathlib
import re
import subprocess
from urllib.parse import urlparse

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "docker-compose.yml"

# `${NAME:?message}` — a variable compose refuses to interpolate without a value.
_REQUIRED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?")

# Values a profile may legitimately ship empty, because only the user knows them.
#
# AVA_MODEL is empty in EVERY profile, not just cloud.env: Ava ships no default
# model, so there is no value a profile could carry that the owner chose.
#
# THIS COMMENT USED TO SAY the `${AVA_MODEL:?}` guard in compose "turns that
# into an instruction at `up` time". That was wrong, and being wrong here is
# what kept CI red for two weeks. `${VAR:?}` fires during INTERPOLATION, which
# `docker compose config` performs on every service in the file — so a variable
# every profile ships empty plus a `:?` guard on it is not a helpful message at
# `up` time, it is a compose file that cannot be validated at all, under any
# profile. This exemption and that guard were in direct contradiction, and only
# the CI job that shells out to `docker compose config` could see it, because
# nothing in this suite runs compose. test_no_required_guard_on_a_variable_
# profiles_ship_empty below now catches it here, without a Docker daemon.
_MAY_BE_EMPTY = {
    "cloud.env": {"AVA_BACKEND_URL", "AVA_MODEL", "AVA_INFERENCE_KEY"},
    "*": {"AVA_MODEL"},
}


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", pattern],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _compose_interpolated_text() -> str:
    """The compose file with its `#` comments removed.

    Everything that scans for `${VAR:?...}` must read THIS, not the raw file.
    docker-compose.yml carries long why-comments that quote the very expansion
    they warn against - "not `--model ${AVA_MODEL:?...}`, because ..." - and a
    scan over the raw text reads the warning as the offence, which is a guard
    that fires on its own documentation. tests/test_landing_page.py strips
    Jinja comments for exactly this reason; same rule, different syntax.

    Compose itself never interpolates a comment, so stripping them is also the
    faithful model of what `docker compose config` actually does.
    """
    out = []
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # An inline trailing comment: ` # ...`. Guarded on the space so a `#`
        # inside a value (a URL fragment, a colour) is left alone.
        cut = line.find(" #")
        out.append(line[:cut] if cut != -1 else line)
    return "\n".join(out)


def _profile_env(rel: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / rel).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _services_started_by(profile: str, compose: dict) -> set[str]:
    started = set()
    for name, svc in (compose.get("services") or {}).items():
        declared = svc.get("profiles")
        if not declared or profile in declared:
            started.add(name)
    return started


def test_profile_files_exist_for_every_declared_profile() -> None:
    compose = _compose()
    declared = set()
    for svc in (compose.get("services") or {}).values():
        declared.update(svc.get("profiles") or [])
    declared.add("cloud")          # starts only the always-on services

    shipped = {pathlib.Path(p).stem for p in _tracked("deploy/profiles/*.env")}
    missing = sorted(declared - shipped)
    extra = sorted(shipped - declared)
    assert not missing and not extra, (
        "deploy/profiles/*.env must match the profiles declared in "
        f"deploy/docker-compose.yml — missing {missing}, unexpected {extra}")


def test_every_required_variable_is_supplied_by_every_profile() -> None:
    required = set(_REQUIRED.findall(_compose_interpolated_text()))
    assert required, (
        "no `${VAR:?...}` guards found in deploy/docker-compose.yml — the backend "
        "trio must stay guarded, or a wrong default silently returns")

    offenders: list[str] = []
    for rel in _tracked("deploy/profiles/*.env"):
        env = _profile_env(rel)
        exempt = (_MAY_BE_EMPTY.get(pathlib.Path(rel).name, set())
                  | _MAY_BE_EMPTY.get("*", set()))
        for var in sorted(required):
            if var not in env:
                offenders.append(f"{rel}: missing {var}")
            elif not env[var] and var not in exempt:
                offenders.append(f"{rel}: {var} is empty")
    assert not offenders, (
        "compose refuses to interpolate these, so a profile that omits one "
        f"cannot start at all — {offenders}")


def test_no_required_guard_on_a_variable_profiles_ship_empty() -> None:
    """`:?` and "ships empty" cannot both be true, and CI is the only witness.

    `${VAR:?msg}` is refused by compose when VAR is unset OR EMPTY, and the
    refusal happens at interpolation — which `docker compose config` runs over
    every service in the file, regardless of which profile is selected. So one
    guarded variable that every shipped profile leaves blank makes the whole
    compose file unvalidatable, for everybody, not just for the profile that
    needs the value.

    That is exactly what happened. `${AVA_MODEL:?}` was correct while the
    profiles shipped `AVA_MODEL=llama3.2`; "ship no default model" emptied them
    all and left both guards standing, and from that commit `docker compose
    config` failed for every profile. Two CI jobs went red and stayed red
    through seven pushes, because the failure lives in a `docker compose` shell
    -out that no test here performs, and _MAY_BE_EMPTY above had talked itself
    into believing `:?` only fires at `up` time.

    This test is the cheap version of that CI job: pure text, no daemon, so the
    contradiction is caught by `pytest tests/` on the machine that writes it.
    """
    guarded = set(_REQUIRED.findall(_compose_interpolated_text()))

    always_empty: dict[str, list[str]] = {}
    for rel in _tracked("deploy/profiles/*.env"):
        env = _profile_env(rel)
        for var in guarded:
            if not env.get(var, ""):
                always_empty.setdefault(var, []).append(pathlib.Path(rel).name)

    # cloud.env is the deliberate exception: it ships its values blank so that
    # compose REFUSES it, which is asserted directly in the compose-config CI
    # job. A variable is only a contradiction when NO profile supplies it.
    shipped = {pathlib.Path(p).name for p in _tracked("deploy/profiles/*.env")}
    offenders = sorted(v for v, files in always_empty.items()
                       if set(files) == shipped)
    assert not offenders, (
        f"deploy/docker-compose.yml guards {offenders} with `${{VAR:?...}}`, and "
        "every deploy/profiles/*.env ships them empty. compose refuses an empty "
        "value at interpolation time, so `docker compose config` fails under "
        "EVERY profile and the stack cannot start at all.\n"
        "Either give the profiles a real value, or drop the guard to "
        "`${VAR:-}` (or `${VAR:+--flag ${VAR}}` for a command argument, so an "
        "empty value omits the flag instead of passing a blank one)."
    )


def test_each_profile_points_at_an_engine_it_actually_starts() -> None:
    """The regression test for the original defect."""
    compose = _compose()
    service_names = set((compose.get("services") or {}).keys())

    offenders: list[str] = []
    for rel in _tracked("deploy/profiles/*.env"):
        env = _profile_env(rel)
        url = env.get("AVA_BACKEND_URL", "")
        if not url:
            continue                     # cloud ships empty on purpose
        host = (urlparse(url).hostname or "")
        if host not in service_names:
            continue                     # an external/cloud endpoint, not ours
        profiles = [p for p in env.get("COMPOSE_PROFILES", "").split(",") if p]
        started: set[str] = set()
        for p in profiles:
            started |= _services_started_by(p, compose)
        if host not in started:
            offenders.append(
                f"{rel}: AVA_BACKEND_URL points at '{host}', which profiles "
                f"{profiles} never start (they start: {sorted(started)})")

    assert not offenders, (
        "these profiles wire the bridge to a compose service the profile does not "
        "bring up, so every chat turn resolves a hostname that does not exist — "
        f"{offenders}")


def test_every_profile_selects_itself() -> None:
    compose = _compose()
    declared = {"cloud"}
    for svc in (compose.get("services") or {}).values():
        declared.update(svc.get("profiles") or [])

    offenders: list[str] = []
    for rel in _tracked("deploy/profiles/*.env"):
        env = _profile_env(rel)
        value = env.get("COMPOSE_PROFILES", "")
        if not value:
            offenders.append(f"{rel}: no COMPOSE_PROFILES (compose would start no engine)")
            continue
        stem = pathlib.Path(rel).stem
        parts = [p for p in value.split(",") if p]
        if stem not in parts:
            offenders.append(f"{rel}: COMPOSE_PROFILES={value!r} does not include '{stem}'")
        for p in parts:
            if p not in declared:
                offenders.append(f"{rel}: COMPOSE_PROFILES names unknown profile '{p}'")
    assert not offenders, (
        "COMPOSE_PROFILES is what makes `docker compose up -d` (with no flags) "
        f"start the right services — {offenders}")


def test_an_accelerator_claim_never_lands_on_a_profile_that_must_start_anywhere() -> None:
    """A service that DEMANDS a device may not run under `cpu`.

    Docker never grants GPU access implicitly, so an accelerated service has to
    declare it — `devices: [/dev/kfd]` for ROCm, a `deploy.resources.reservations`
    entry for NVIDIA. The cost of declaring it is that the service then cannot
    start at all on a host that cannot satisfy it.

    `cpu` is the profile for every machine with no usable GPU, which is the
    entire population it exists for. So the accelerated variants are separate
    services (`ollama-cuda`, `ollama-rocm`) rather than a block bolted onto the
    shared `ollama`, and this is the guard that keeps them separate — the
    tempting one-line "just add a reservation to ollama" breaks every GPU-less
    install, and it breaks it at `docker compose up`, before any log exists.
    """
    compose = _compose()
    offenders: list[str] = []
    for name, svc in (compose.get("services") or {}).items():
        reserves = [
            d for d in (((svc.get("deploy") or {}).get("resources") or {})
                        .get("reservations") or {}).get("devices") or []
        ]
        claims = bool(svc.get("devices")) or bool(reserves) or bool(svc.get("runtime"))
        if not claims:
            continue
        declared = svc.get("profiles")
        if not declared:
            offenders.append(f"{name}: claims a device but declares no profiles, "
                             "so it starts under every one of them")
        elif "cpu" in declared:
            offenders.append(f"{name}: claims a device and runs under the cpu profile")
    assert not offenders, (
        "these services demand hardware the cpu profile's machines do not have, "
        "so compose refuses to start them and the install dies before the app "
        f"ever runs — {offenders}")


def test_every_ollama_profile_has_its_own_service_and_shares_the_model_cache() -> None:
    """Switching accelerators must not re-download the weights.

    `ollama`, `ollama-cuda` and `ollama-rocm` are three services because their
    device declarations differ, not because their model stores do. Pointing them
    at different volumes would make `cpu -> cuda` a multi-GB re-download of
    bytes already on disk, which is the kind of cost nobody attributes to the
    profile switch that caused it.
    """
    compose = _compose()
    services = compose.get("services") or {}
    ollamas = {n: s for n, s in services.items()
               if str(s.get("image", "")).startswith("ollama/ollama")}
    assert len(ollamas) >= 2, (
        "expected at least the shared `ollama` service and one accelerated "
        f"variant, found {sorted(ollamas)}")

    stores = {n: [v for v in (s.get("volumes") or []) if "/root/.ollama" in str(v)]
              for n, s in ollamas.items()}
    empty = sorted(n for n, v in stores.items() if not v)
    assert not empty, f"these Ollama services mount no model store: {empty}"
    distinct = {tuple(v) for v in stores.values()}
    assert len(distinct) == 1, (
        "the Ollama services mount different model stores, so switching profiles "
        f"re-downloads weights that are already on disk — {stores}")


def test_the_bridge_is_told_the_host_gpu_it_cannot_see() -> None:
    """install.sh measures the host GPU; the bridge container must receive it.

    Compose reserves the device for the inference service only, so nothing in the
    `ava` container can discover the card — its own probes correctly find none.
    The measurement therefore has to arrive as a DECLARED value or the truth
    never crosses the boundary, which is why Setup → Hardware could only ever say
    "no GPU under Docker" on a machine with a working one.
    """
    ava = (_compose().get("services") or {}).get("ava") or {}
    env = ava.get("environment") or {}
    missing = [k for k in ("AVA_DECLARED_HOST_GPU_NAME",
                           "AVA_DECLARED_HOST_GPU_VRAM_MIB") if k not in env]
    assert not missing, (
        "the ava service no longer receives the host GPU deploy/install.sh "
        f"measured, so the container cannot report it at all — {missing}")

    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    unwritten = [k for k in ("AVA_DECLARED_HOST_GPU_NAME",
                             "AVA_DECLARED_HOST_GPU_VRAM_MIB")
                 if f"{k}=%s" not in install]
    assert not unwritten, (
        "deploy/install.sh no longer writes these into its managed .env block, "
        "so compose interpolates the empty default and the reading it took on "
        f"the host dies as a shell local again — {unwritten}")


def test_the_bridge_port_is_published_on_loopback_only() -> None:
    ava = (_compose().get("services") or {}).get("ava") or {}
    ports = [str(p) for p in (ava.get("ports") or [])]
    assert ports, "the ava service publishes no port"
    offenders = [p for p in ports if not p.startswith("127.0.0.1:")]
    assert not offenders, (
        "the bridge must publish on 127.0.0.1 only. It binds 0.0.0.0 INSIDE its "
        "namespace (deploy/Dockerfile), so a 0.0.0.0 publish here would expose an "
        f"unauthenticated first-run /setup to the whole network — {offenders}")


def test_both_serving_paths_honour_the_model_revision_pin() -> None:
    """`models.<role>.revision` pins what `ava models pull` DOWNLOADS. Without
    the same pin on the server, vLLM resolves the model repo's default branch on
    every start — so the same config serves different weights the day upstream
    pushes, and nothing anywhere reports the change.

    There are two serving paths and they are edited independently: the compose
    `vllm` service (the documented Docker install) and `deploy/local-serve.sh`
    (bare metal). A pin honoured by one and not the other is worse than no pin,
    because the owner has no way to tell which of their boxes is actually pinned.
    """
    raw = ((_compose().get("services") or {}).get("vllm") or {}).get("command", "")
    # `command:` is a folded scalar here, but compose also accepts a list — join
    # a list, keep a string. Joining a STRING iterates its characters, which
    # spaces out every token and makes every assertion below pass or fail for
    # the wrong reason.
    cmd = " ".join(str(x) for x in raw) if isinstance(raw, list) else str(raw)
    assert "AVA_MODEL_REVISION" in cmd, (
        "the compose vllm service ignores AVA_MODEL_REVISION, so a pinned model "
        "is downloaded at one commit and served from whatever the branch tip is")
    assert "--revision" in cmd, "the revision is read but never passed to vLLM"

    serve = (ROOT / "deploy" / "local-serve.sh").read_text(encoding="utf-8")
    assert "AVA_MODEL_REVISION" in serve, (
        "deploy/local-serve.sh ignores AVA_MODEL_REVISION — the bare-metal path "
        "is unpinnable while the Docker path is pinned")
    assert "--revision" in serve

    # The tokenizer too, on both. vLLM's --tokenizer-revision defaults to None,
    # which resolves the tokenizer repo's own branch tip even when the weights
    # are pinned — and half a pin reads exactly like a whole one.
    assert "--tokenizer-revision" in cmd, "compose pins the weights, not the tokenizer"
    assert "--tokenizer-revision" in serve, "local-serve pins the weights, not the tokenizer"


def test_every_vllm_profile_documents_the_pin() -> None:
    """A knob nobody can find is a knob nobody sets. The profiles are where an
    owner names their model, so it is where the commit pin belongs too."""
    missing = []
    for env in sorted((ROOT / "deploy" / "profiles").glob("*.env")):
        text = env.read_text(encoding="utf-8")
        # Only the profiles that actually serve with vLLM.
        if "AVA_BACKEND_ENGINE=vllm" not in text:
            continue
        if "AVA_MODEL_REVISION" not in text:
            missing.append(env.name)
    assert not missing, (
        "these vLLM profiles name a model but offer no way to pin its commit, "
        f"so the model they serve can change under them: {missing}")
