"""`deploy/default-model.env` is the ONE place the shipped default model lives.

That file says so in as many words — "Every tracked default in the repo is asserted
equal to this value by tests/test_default_model_pin.py, so swapping the shipped
default is a one-line edit here plus whatever that test tells you to update."

**This file did not exist.** The comment promised a guard that was never written, so
the default was duplicated across nine tracked files with nothing holding them
together, and "a one-line edit" was false: change `default-model.env` alone and
`config.example.yaml`, `models.py`, `router_app.py`, `voice_ava.py` and three docs
sites keep serving the old value.

The sizing tests below exist for a related reason: two of those files disagreed
about whether the default fits a 16 GB card.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Every tracked file that repeats the default. A new one added without landing here
# is exactly the drift the comment claims is impossible.
ECHOES = (
    "config.example.yaml",
    "deploy/README.md",
    "ava_bridge/models.py",
    "ava_bridge/router_app.py",
    "voice_ava.py",
)


def shipped_default() -> str:
    """The value in the single source of truth."""
    for line in (ROOT / "deploy" / "default-model.env").read_text(
            encoding="utf-8").splitlines():
        if line.startswith("AVA_MODEL="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("deploy/default-model.env declares no AVA_MODEL")


def test_the_single_source_of_truth_parses() -> None:
    m = shipped_default()
    assert "/" in m and len(m) > 6, f"implausible default model: {m!r}"


def test_every_tracked_echo_carries_the_current_default() -> None:
    """The guard `default-model.env` promised, stated the way it is useful.

    Not "no other model may be mentioned" — `config.example.yaml` legitimately shows
    `Qwen3-32B-AWQ` as a scale-up example and `deploy/README.md` names the 3B a
    small card downshifts to. The property that matters is the inverse: change the
    source of truth and this names every file still holding the OLD value, which is
    what makes "a one-line edit here plus whatever the test tells you" true.

    My first version scanned for any Qwen id and flagged both legitimate cases —
    a guard that cries wolf is one people delete.
    """
    want = shipped_default()
    missing = [rel for rel in ECHOES
               if want not in (ROOT / rel).read_text(encoding="utf-8")]
    assert not missing, (
        f"the shipped default is {want!r} and these tracked files do not mention "
        f"it: {missing}. Each of them hardcodes a default of its own, so changing "
        "deploy/default-model.env alone leaves them serving the previous model — "
        "silently, because each file is internally consistent.")


def test_the_sizing_gate_and_the_model_doc_do_not_contradict_each_other() -> None:
    """Two shipped files disagreed about whether the default fits a 16 GB card.

    `deploy/default-model.env` said 7B "is the largest model that reliably fits the
    16 GB card deploy/install.sh green-lights", while `deploy/README.md` said the
    same model needs ~20 GB and that install.sh only drops to cpu below 16 GB. Both
    were tracked, both were read by forkers, and they could not both be true — the
    arithmetic (14.2 GiB of weights against 0.90 x 16376 MiB = 14.4 GiB usable)
    says the README was right.

    This asserts the three numbers now agree across install.sh, default-model.env
    and deploy/README.md, because a contradiction between two docs is exactly the
    defect a reader cannot resolve on their own.
    """
    root = ROOT
    sh = (root / "deploy" / "install.sh").read_text(encoding="utf-8")
    env = (root / "deploy" / "default-model.env").read_text(encoding="utf-8")
    readme = (root / "deploy" / "README.md").read_text(encoding="utf-8")

    m = re.search(r"_need_default_mib=(\d+)", sh)
    assert m, "install.sh no longer declares _need_default_mib"
    need = int(m.group(1))
    assert need >= 17000, (
        f"the sizing gate is {need} MiB. 0.90 x 16376 MiB leaves 14.4 GiB against "
        "14.2 GiB of weights — 0.2 GiB, with a 32k KV cache still to place. A gate "
        "at or below 16 GB green-lights an OOM.")

    gib = need / 1024
    assert f"~{gib:.0f} GB" in readme or f"{gib:.0f} GB" in readme, (
        f"deploy/README.md does not state the {gib:.0f} GB requirement install.sh "
        "enforces, so the two can drift apart again.")

    assert "reliably fits the 16 GB card" not in env, (
        "default-model.env claims the default fits a 16 GB card. It does not — see "
        "the arithmetic in install.sh's sizing block.")
    assert "does NOT fit a 16 GB card" in env, (
        "default-model.env no longer records that the default does not fit 16 GB, "
        "which is the correction this test exists to hold.")


def test_the_small_card_downshift_names_a_model_the_flags_table_knows() -> None:
    """A downshift to a model with no tool parser row would trade an OOM for an
    agent whose every tool call silently returns no tool_calls."""
    root = ROOT
    sh = (root / "deploy" / "install.sh").read_text(encoding="utf-8")
    m = re.search(r'AVA_MODEL="(Qwen/[^"]+)"', sh)
    assert m, "install.sh no longer names a downshift model"
    small = m.group(1)
    flags = (root / "deploy" / "model-flags.conf").read_text(encoding="utf-8")
    family = small.split("/")[-1].split("-")[0]      # Qwen2.5
    assert family in flags, (
        f"the downshift model {small} has no family row in model-flags.conf, so it "
        "would serve with no --tool-call-parser and every tool call would come "
        "back empty.")
