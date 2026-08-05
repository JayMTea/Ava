"""The staging slot must stay a SEPARATE stack, not a second view of the live one.

A slot (deploy/slot.yml + slot.sh + slot.env.example) runs a second Ava beside the
running one so a build can be tested while the stable instance keeps serving. It
separates four things, and every one of them is silent when it breaks:

  * the compose project   — without `-p`, `up` RECREATES the stable containers
  * AVA_HOME              — two bridges over one SQLite store and one session key
  * the published port    — a merge that APPENDS rather than replaces cannot bind
  * AVA_IMAGE             — `image:` is both the build output tag and the run
                            reference, so a slot build retags what stable
                            restarts into; the running container keeps serving
                            from its old image ID, so nothing looks wrong until
                            the next restart

Two of those are one character away from regressing. Dropping the `!override` tag
makes compose append the base file's `127.0.0.1:8096:8096` alongside the slot's,
and renaming the compose project detaches the slot from the engine it borrows —
neither raises anything at parse time. This file pins them.

Style matches tests/test_diagram_sync.py: a `git ls-files` scan, no bridge, no
AVA_HOME, no Docker daemon.
"""
import pathlib
import re
import subprocess

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
SLOT = ROOT / "deploy" / "slot.yml"
SLOT_ENV = ROOT / "deploy" / "slot.env.example"
SLOT_SH = ROOT / "deploy" / "slot.sh"

# `${NAME:?message}` — a variable compose refuses to interpolate without a value.
_REQUIRED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?")
# The default in `${NAME:-default}`.
_DEFAULTED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}")


class _TagTolerantLoader(yaml.SafeLoader):
    """SafeLoader that keeps going past compose merge tags (`!override`, `!reset`).

    They are compose directives, not YAML types, so a plain safe_load raises on
    slot.yml. The tag itself is asserted against the raw text below — losing it
    here only means the parsed value is the plain sequence, which is what the
    structural assertions want anyway.
    """


def _merge_tag(loader, suffix, node):
    # `suffix` is unused: PyYAML fixes this signature for multi-constructors.
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_TagTolerantLoader.add_multi_constructor("!", _merge_tag)


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", pattern],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def _yaml(path: pathlib.Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_TagTolerantLoader)


def _slot_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in SLOT_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _host_port(entry: str) -> str:
    """Host port from a `[ip:]host:container` published-port string."""
    return entry.strip('"').split(":")[-2]


def test_the_slot_files_are_tracked() -> None:
    """A fresh clone must get all three, or `slot.sh` is a broken path."""
    for rel in ("deploy/slot.yml", "deploy/slot.sh", "deploy/slot.env.example"):
        assert _tracked(rel) == [rel], (
            f"{rel} is not in the git index — run `git add {rel}`. "
            "deploy/local-serve.sh once shipped untracked while another script "
            "exec'd it; tests/test_deploy_refs_tracked.py exists for the same reason.")


def test_the_slot_replaces_the_published_port_instead_of_adding_one() -> None:
    """Without `!override`, compose APPENDS and the slot inherits 8096 as well.

    That is the whole mechanism, it is one word long, and dropping it produces a
    slot that either refuses to bind or — if stable happens to be down — silently
    takes production's port.
    """
    src = SLOT.read_text(encoding="utf-8")
    assert re.search(r"ports:\s*!override\b", src), (
        "deploy/slot.yml must publish its port with `ports: !override`. Compose's "
        "default merge for a sequence appends, so without the tag the slot also "
        "inherits the base file's \"127.0.0.1:8096:8096\" and collides with the "
        "stable stack.")

    slot_ports = _yaml(SLOT)["services"]["ava"]["ports"]
    assert len(slot_ports) == 1, f"the slot must publish exactly one port, got {slot_ports}"

    entry = str(slot_ports[0])
    assert entry.strip('"').startswith("127.0.0.1:"), (
        f"the slot must publish on loopback only, got {entry!r} — the same rule "
        "tests/test_dockerfile_bind.py enforces for the base file: an "
        "unauthenticated first-run /setup must not be reachable from the LAN.")

    base_ports = [str(p) for p in _yaml(COMPOSE)["services"]["ava"]["ports"]]
    default = _DEFAULTED.search(entry)
    assert default and default.group(1) == "AVA_PORT_HOST", (
        f"the slot's host port must come from ${{AVA_PORT_HOST:-…}}, got {entry!r}")
    assert default.group(2) not in {_host_port(p) for p in base_ports}, (
        f"the slot's default host port {default.group(2)} is the one the stable "
        f"stack already publishes ({base_ports}) — they cannot both bind it.")


def test_the_slot_passes_through_what_the_base_environment_allowlist_omits() -> None:
    """An env-file value interpolates; it does not reach the container.

    docker-compose.yml's `environment:` is an explicit allowlist, so setting
    AVA_NAME or AVA_ALLOC_INFER in .env.staging alone does nothing at all — and
    does it quietly. AVA_ALLOC_INFER is the one that matters: left unset it
    defaults true, handing the slot a live unload lever aimed at the engine it
    borrows from production.
    """
    base_env = _yaml(COMPOSE)["services"]["ava"].get("environment") or {}
    slot_env = _yaml(SLOT)["services"]["ava"].get("environment") or {}
    for key in ("AVA_NAME", "AVA_ALLOC_INFER"):
        assert key in base_env or key in slot_env, (
            f"{key} reaches the container from neither deploy/docker-compose.yml "
            f"nor deploy/slot.yml, so setting it in .env.staging is inert. Name it "
            f"under `environment:` in slot.yml.")

    infer = str(slot_env.get("AVA_ALLOC_INFER", ""))
    assert _DEFAULTED.search(infer) and _DEFAULTED.search(infer).group(2) == "0", (
        f"slot.yml must default AVA_ALLOC_INFER to 0, got {infer!r}. A slot shares "
        "the stable stack's engine; with levers inferred it can unload a model out "
        "from under production chat, from a button that renders on every screen.")


def test_the_slot_joins_the_stable_projects_network_by_its_real_name() -> None:
    """The borrowed-engine link is a string that no parser checks.

    The slot reaches the running engine by service DNS over the stable project's
    default network. That network is named `<project>_default`, and the project
    name lives in docker-compose.yml — so renaming it there silently detaches the
    slot, which then boots healthy and fails every chat turn.
    """
    compose = _yaml(COMPOSE)
    project = compose.get("name")
    assert project, "deploy/docker-compose.yml lost its top-level `name:`"

    nets = _yaml(SLOT).get("networks") or {}
    external = {k: v for k, v in nets.items() if (v or {}).get("external")}
    assert external, (
        "deploy/slot.yml declares no external network, so the slot cannot reach "
        "the stable stack's engine. host.docker.internal is not an alternative: "
        "the engine publishes on 127.0.0.1 only and Docker leaves route_localnet=0, "
        "so the host-gateway address gets a connection refused — silently, at the "
        "first chat turn rather than at boot.")

    names = {(v or {}).get("name") for v in external.values()}
    assert names == {f"{project}_default"}, (
        f"deploy/slot.yml joins {names}, but docker-compose.yml's project is "
        f"{project!r}, whose default network is {project}_default.")


def test_the_slot_template_is_self_contained() -> None:
    """`--env-file` REPLACES deploy/.env; it does not layer on top of it.

    So a template listing only the deltas cannot start: compose refuses on the
    first `${VAR:?}` it cannot interpolate — including AVA_MODEL, which is
    required even though a slot never starts a vLLM container, because compose
    interpolates every service regardless of selected profiles.
    """
    required = set(_REQUIRED.findall(COMPOSE.read_text(encoding="utf-8")))
    env = _slot_env()
    missing = sorted(required - env.keys())
    assert not missing, (
        f"deploy/slot.env.example is missing {missing}. Every ${{VAR:?}} in "
        "docker-compose.yml must be present, because --env-file replaces .env "
        "rather than layering on it.")

    for key in ("AVA_HOME", "AVA_IMAGE", "AVA_PORT_HOST", "AVA_ALLOC_INFER"):
        assert env.get(key), (
            f"deploy/slot.env.example must set {key} — it is one of the values "
            "that MUST differ from the stable stack.")

    assert env.get("COMPOSE_PROFILES") == "", (
        "deploy/slot.env.example must set COMPOSE_PROFILES empty: a slot starts no "
        "engine containers, it borrows the stable stack's. A second engine would "
        "re-download the weights and, on the gpu profile, claim the GPU twice.")

    base = _yaml(COMPOSE)["services"]["ava"]
    base_image = _DEFAULTED.search(str(base.get("image", "")))
    if base_image:
        assert env["AVA_IMAGE"] != base_image.group(2), (
            f"deploy/slot.env.example sets AVA_IMAGE={env['AVA_IMAGE']}, the same "
            "tag docker-compose.yml builds by default — a slot build would retag "
            "the image the stable stack restarts into.")

    base_home = _DEFAULTED.search(str(base.get("volumes", [""])[0]))
    if base_home:
        assert env["AVA_HOME"] != base_home.group(2), (
            f"deploy/slot.env.example sets AVA_HOME={env['AVA_HOME']}, the same "
            "directory the stable stack uses — two bridges would share one "
            "database and one session-signing key.")


def test_the_wrapper_pins_every_flag_that_selects_the_stack() -> None:
    """A subcommand missing any of the four addresses the WRONG stack.

    deploy/profiles/README.md makes this argument for COMPOSE_PROFILES already: a
    `down` or `logs` typed without them interpolates a different environment. Here
    the other stack is the production one, so the wrapper must never let a
    subcommand through without all four.
    """
    src = SLOT_SH.read_text(encoding="utf-8")
    body = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    for flag in ("-f docker-compose.yml", "-f slot.yml", "--env-file", "-p "):
        assert flag in body, (
            f"deploy/slot.sh no longer pins `{flag}`. Every subcommand must carry "
            "all four, or it addresses the stable stack instead of the slot.")

    assert "docker compose" in body, "deploy/slot.sh must delegate to `docker compose`"


def test_the_wrapper_can_check_its_own_slot() -> None:
    """`up` that only starts things leaves the verifying to a human who forgets.

    The check that earns this subcommand is the version one: `up` without
    `--build` is the commonest slot mistake there is, and it leaves you signing
    off a container that never contained the change. Nothing else catches it —
    the container is healthy, the UI loads, and the code is last week's.
    """
    src = SLOT_SH.read_text(encoding="utf-8")
    body = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))

    assert "cmd_smoke()" in body, "deploy/slot.sh lost its `smoke` subcommand"
    assert re.search(r"^\s*smoke\)", body, re.M), (
        "cmd_smoke exists but nothing dispatches to it — add `smoke)` to the case.")

    assert "/api/health" in body, (
        "smoke must ask the slot's own port whether it is serving.")
    assert "_stamp" in body and "version" in body, (
        "smoke must compare /api/health's version against _stamp(), or it cannot "
        "tell a fresh build from the image that was already there.")
    assert "/api/hub/agent/inference" in body, (
        "smoke must ask whether Ava can actually ANSWER. A slot detached from the "
        "borrowed engine boots healthy, serves the UI and passes its healthcheck — "
        "the failure slot.yml's `networks:` note describes is invisible to every "
        "other check here.")


def test_a_smoke_that_cannot_identify_the_instance_fails_rather_than_skips() -> None:
    """A green run against something that is not Ava is worse than no run.

    Observed: the default slot port was already held by an unrelated local
    service that answers `{"ok":true}` on /api/health. Reachability passed,
    health passed, and an earlier draft SKIPPED the identity check for want of a
    version — so the whole run reported success against a different application.
    `ava_bridge/version.py` ends in `or "0.0.0+dev"`, so a missing version is
    never a shy Ava; it is not Ava.
    """
    src = SLOT_SH.read_text(encoding="utf-8")
    m = re.search(r'if \[ -z "\$version" \]; then\s*\n\s*(\w+) ', src)
    assert m, "smoke no longer branches on an absent version at all"
    assert m.group(1) == "fail", (
        f"an absent version routes to `{m.group(1)}`, not `fail`. A health payload "
        "with no version is a different application answering on the slot's port.")


def test_the_slot_default_port_is_stated_in_one_place() -> None:
    """Two copies of a default drift, and the drift is a port collision.

    `cmd_init` writes AVA_PORT_HOST into the env file and `cmd_claim`/`cmd_smoke`
    read it back; the fallback they use when it is absent must be the same number
    the template ships, or a hand-written env file sends them to different ports.
    """
    src = SLOT_SH.read_text(encoding="utf-8")
    fallbacks = set(re.findall(r'AVA_PORT_HOST[:-]*\}?["\']?\s*;?\s*\w*="\$\{?\w*:-(\d{4})\}?"', src))
    fallbacks |= set(re.findall(r'port="\$\{port:-(\d{4})\}"', src))
    fallbacks |= set(re.findall(r'local port="\$\{AVA_PORT_HOST:-(\d{4})\}"', src))
    assert len(fallbacks) <= 1, (
        f"deploy/slot.sh has more than one default host port: {sorted(fallbacks)}. "
        "They must agree, or `claim` and `smoke` address different ports than "
        "`init` wrote.")
    if fallbacks:
        env_port = _slot_env().get("AVA_PORT_HOST", "")
        assert env_port == fallbacks.pop(), (
            "deploy/slot.env.example's AVA_PORT_HOST disagrees with slot.sh's "
            "fallback — a hand-written env file would land on a different port.")
