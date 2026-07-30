"""The Node major is pinned in exactly ONE place: frontend/.nvmrc.

deploy/Dockerfile's web stage used to say `FROM node:20-slim` while
frontend/.nvmrc said 22. That is not a cosmetic disagreement. `npm run build`
runs frontend/scripts/ensure-deps.mjs as a prebuild, which reads .nvmrc and
refuses a mismatched major on purpose — because CI's frontend-dist-drift job
rebuilds the bundle on the pinned major and byte-compares it against the
committed frontend/dist, so building on a different major produces a diff the
author did not make. So the mismatch did not ship a subtly-wrong image: it
failed compose-smoke's "Build the bridge image" step, on every PR, with an
error message about a dist diff.

ensure-deps.mjs already catches this inside the image build (.dockerignore does
not exclude .nvmrc, so `COPY frontend/ ./` carries it into the web stage). This
guard exists because that check costs an `npm ci` and a multi-minute image
build to report, and because it cannot see the two pins that live outside the
frontend at all: deploy/agent.Dockerfile installs Node from NodeSource, and one
setup-node step in .github/workflows/ci.yml carries a literal major instead of
`node-version-file`.

Reading the pins is done by walking Dockerfile INSTRUCTIONS (comments stripped,
continuations joined, global ARGs resolved) and by parsing the workflow YAML —
never by substring-scanning the file text. A text scan of deploy/Dockerfile
would match the explanatory comment above its own ARG, and this repo has
already shipped guards that passed by matching their own prose.

Style matches tests/test_diagram_sync.py: a `git ls-files` scan, no bridge, no
AVA_HOME, no Docker daemon.
"""
import pathlib
import re

import yaml
from gitfiles import tracked as _tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]
NVMRC = ROOT / "frontend" / ".nvmrc"

# `node:22-slim`, `node:22.12.0-alpine`, `docker.io/library/node:22`.
_NODE_IMAGE = re.compile(r"^(?:[\w.\-]+\.[\w.\-]+/)?(?:library/)?node:(\S+)$")
# `curl -fsSL https://deb.nodesource.com/setup_22.x | bash -`
_NODESOURCE = re.compile(r"deb\.nodesource\.com/setup_(\d+)")
_LEADING_MAJOR = re.compile(r"^v?(\d+)")

_FIX = ("frontend/.nvmrc is the single Node pin — ensure-deps.mjs and CI's "
        "node-version-file both read it. Update the copy to match .nvmrc "
        "(or move .nvmrc first and then every copy below).")


def _nvmrc_major() -> str:
    raw = NVMRC.read_text(encoding="utf-8").strip()
    m = _LEADING_MAJOR.match(raw)
    assert m, f"frontend/.nvmrc does not start with a version: {raw!r}"
    return m.group(1)


def _instructions(src: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, argument) pairs from Dockerfile text.

    Comment lines are dropped and backslash continuations joined, so a RUN body
    is one string and a comment can never be mistaken for an instruction.
    """
    out: list[tuple[str, str]] = []
    buf = ""
    for raw in src.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue                      # dropped inside continuations too
        if not buf and not line:
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        parts = buf.split(None, 1)
        if parts:
            out.append((parts[0].upper(), parts[1].strip() if len(parts) > 1 else ""))
        buf = ""
    parts = buf.split(None, 1)
    if parts:
        out.append((parts[0].upper(), parts[1].strip() if len(parts) > 1 else ""))
    return out


def _global_args(instrs: list[tuple[str, str]]) -> dict[str, str]:
    """ARG defaults declared before the first FROM — the only ones a FROM sees."""
    args: dict[str, str] = {}
    for op, arg in instrs:
        if op == "FROM":
            break
        if op == "ARG":
            for tok in arg.split():
                if "=" in tok:
                    name, value = tok.split("=", 1)
                    args[name] = value.strip("\"'")
    return args


def _expand(text: str, args: dict[str, str]) -> str:
    for name, value in args.items():
        text = text.replace("${" + name + "}", value).replace("$" + name, value)
    return text


def _from_images(instrs: list[tuple[str, str]], args: dict[str, str]) -> list[str]:
    """The image reference of every FROM, with `--platform=…` flags dropped."""
    images = []
    for op, arg in instrs:
        if op != "FROM":
            continue
        toks = [t for t in _expand(arg, args).split() if not t.startswith("--")]
        if toks:
            images.append(toks[0])
    return images


def _dockerfiles() -> list[str]:
    return [rel for rel in _tracked("*Dockerfile*") if "node_modules" not in rel]


def test_dockerfile_node_base_images_match_nvmrc() -> None:
    want = _nvmrc_major()
    seen: list[str] = []
    offenders: list[str] = []
    for rel in _dockerfiles():
        instrs = _instructions((ROOT / rel).read_text(encoding="utf-8"))
        args = _global_args(instrs)
        for image in _from_images(instrs, args):
            m = _NODE_IMAGE.match(image)
            if not m:
                continue
            seen.append(f"{rel}: {image}")
            major = _LEADING_MAJOR.match(m.group(1))
            if not major:
                offenders.append(
                    f"{rel}: `FROM {image}` does not name a major (unpinned tag)")
            elif major.group(1) != want:
                offenders.append(
                    f"{rel}: `FROM {image}` is Node {major.group(1)}, .nvmrc pins {want}")
    assert seen, ("no `FROM node:…` found in any tracked Dockerfile — the web "
                  "build stage moved and this guard is now watching nothing")
    assert not offenders, (
        "these Dockerfile base images disagree with frontend/.nvmrc, so "
        "`npm run build` in the image aborts in ensure-deps.mjs and the image "
        f"never builds. {_FIX} — {offenders}")


def test_nodesource_installs_match_nvmrc() -> None:
    """deploy/agent.Dockerfile installs Node from NodeSource rather than a base
    image, so `setup_NN.x` is a third copy of the same number."""
    want = _nvmrc_major()
    offenders: list[str] = []
    for rel in _dockerfiles():
        instrs = _instructions((ROOT / rel).read_text(encoding="utf-8"))
        args = _global_args(instrs)
        for op, arg in instrs:
            if op not in {"RUN", "ENV", "ARG"}:
                continue
            for major in _NODESOURCE.findall(_expand(arg, args)):
                if major != want:
                    offenders.append(
                        f"{rel}: NodeSource setup_{major}.x, .nvmrc pins {want}")
    assert not offenders, (
        "these install Node from a NodeSource channel that is not the pinned "
        f"major. {_FIX} — {offenders}")


def test_setup_node_steps_take_their_version_from_nvmrc() -> None:
    """A workflow should say `node-version-file: frontend/.nvmrc`. A literal
    `node-version:` is a copy, and is allowed only while it still agrees."""
    want = _nvmrc_major()
    rel_nvmrc = "frontend/.nvmrc"
    steps_seen = 0
    offenders: list[str] = []

    for rel in _tracked(".github/workflows/*.yml"):
        doc = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8")) or {}
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                uses = str((step or {}).get("uses") or "")
                if not uses.startswith("actions/setup-node"):
                    continue
                steps_seen += 1
                where = f"{rel}:{job_name}"
                with_ = (step.get("with") or {})
                vfile = with_.get("node-version-file")
                literal = with_.get("node-version")
                if vfile is not None:
                    if str(vfile).strip() != rel_nvmrc:
                        offenders.append(
                            f"{where}: node-version-file is {vfile!r}, not {rel_nvmrc!r}")
                elif literal is None:
                    offenders.append(
                        f"{where}: setup-node pins nothing — add "
                        f"`node-version-file: {rel_nvmrc}`")
                elif "${{" in str(literal):
                    offenders.append(
                        f"{where}: node-version is the expression {literal!r}, which "
                        f"no static check can compare — use `node-version-file: {rel_nvmrc}`")
                else:
                    m = _LEADING_MAJOR.match(str(literal).strip())
                    if not m:
                        offenders.append(f"{where}: node-version {literal!r} names no major")
                    elif m.group(1) != want:
                        offenders.append(
                            f"{where}: node-version {literal!r} is Node {m.group(1)}, "
                            f".nvmrc pins {want}")

    assert steps_seen, ("no actions/setup-node step found in any workflow — the "
                        "frontend jobs moved and this guard watches nothing")
    assert not offenders, (
        "these setup-node steps do not resolve to the pinned major, so a job "
        "can rebuild frontend/dist on a different interpreter than the one that "
        f"produced the committed bundle. {_FIX} — {offenders}")
