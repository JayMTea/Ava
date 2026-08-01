"""The documented install steps must match what the CLI actually prints.

Both install paths shipped broken for the same reason: `ava up` starts the WEB
APP and never an inference engine, but README.md, deploy/README.md and
`ava setup`'s own closing line each listed their own shortened sequence, and not
one of them included the engine. A forker followed three steps, got a working
login page, and the first chat message returned a raw 503.

The steps live in three places for good reasons (two audiences plus the terminal
you are already looking at), so this does not try to collapse them — it just
fails when they disagree about something load-bearing.

Same shape as tests/test_diagram_sync.py: a static scan over tracked files that
runs anywhere, with no bridge, no AVA_HOME and no network.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Each step: (regex that must appear, why it is load-bearing).
_REQUIRED = [
    (re.compile(r"ava models pull --auto"),
     "the model download — without it nothing has weights to serve"),
    (re.compile(r"local-serve\.sh|ollama serve"),
     "the inference engine launch — `ava up` never starts one"),
    (re.compile(r"\bava doctor\b"),
     "the readiness check that stops the chain when nothing can answer"),
    (re.compile(r"\bava up\b"),
     "starting the web app"),
]

# Files that walk a reader from a clean checkout to a working chat.
_INSTALL_DOCS = ["README.md", "deploy/README.md"]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_install_docs_list_every_bare_metal_step() -> None:
    missing: list[str] = []
    for rel in _INSTALL_DOCS:
        text = _read(rel)
        for pattern, why in _REQUIRED:
            if not pattern.search(text):
                missing.append(f"{rel}: missing {pattern.pattern!r} — {why}")
    assert not missing, (
        "the bare-metal install docs dropped a step. A reader who follows them "
        "must reach a chat box that can actually answer:\n  - "
        + "\n  - ".join(missing))


def test_cli_setup_prints_the_same_steps() -> None:
    """`ava setup`'s closing 'Next:' block is the third copy of this sequence."""
    text = _read("ava_cli.py")
    marker = "Setup complete."
    assert marker in text, "ava_cli.py no longer prints a 'Setup complete.' block"
    tail = text.split(marker, 1)[1][:1200]
    missing = [f"{p.pattern!r} — {why}" for p, why in _REQUIRED if not p.search(tail)]
    assert not missing, (
        "`ava setup`'s next-steps block disagrees with the install docs. Keep "
        "them in sync or a forker follows whichever one they happened to read:"
        "\n  - " + "\n  - ".join(missing))


def test_doctor_can_fail_when_nothing_serves() -> None:
    """The chain only stops if doctor actually exits non-zero — assert the
    contract exists, since a `return 0` here is what made the docs' `&&` useless."""
    text = _read("ava_cli.py")
    assert "No inference backend is reachable" in text, (
        "ava doctor no longer reports the no-inference case; the documented "
        "`ava doctor && ava up` chain silently continues into a broken chat box")
    assert "return 2" in text, (
        "ava doctor no longer exits non-zero when nothing can serve a chat turn")


def test_every_ollama_backed_profile_pulls_its_model() -> None:
    """The pull was gated on `AVA_PROFILE = "cpu"`, but rocm is equally
    Ollama-backed — profiles/rocm.env sets AVA_BACKEND_ENGINE=ollama and the same
    AVA_MODEL — just on the `ollama-rocm` service. So every AMD install finished
    with an empty model store and a chat box wired to a server holding nothing,
    which is the exact failure the comment above that gate says it prevents.

    Nobody hit it because AMD is ci-simulated: no runner has the hardware and the
    maintainer owns none, so the whole profile is exercised by simulation only.
    That is precisely the class of bug a static check is for.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    install = (root / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert 'if [ "$AVA_PROFILE" = "cpu" ]; then' not in install, (
        "the model pull is gated on the cpu profile alone again — rocm serves "
        "through Ollama too and will finish with an empty model store")

    # Each Ollama-backed profile must name the compose service that holds ITS
    # store; `ollama` does not exist under the rocm profile.
    for profile, service in (("cpu", "ollama"), ("rocm", "ollama-rocm")):
        env = (root / "deploy" / "profiles" / f"{profile}.env").read_text(encoding="utf-8")
        if "AVA_BACKEND_ENGINE=ollama" not in env:
            continue          # profile stopped using Ollama; nothing to pull
        assert service in install, (
            f"profiles/{profile}.env serves through Ollama, but install.sh never "
            f"names the {service!r} service, so its model store stays empty")
