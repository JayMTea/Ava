"""The code that writes `code.approval` must not be auto-editable by the agent.

`access_policy._DENY` puts `ava.yaml*` off-limits with the reason stated inline:
"holds code.approval — writable = self-ungating". That is correct and it is only
half the sentence. A denied FILE whose WRITER is auto-editable is not denied —
the same lesson the `ava.yaml*` glob already learned about `ava.yaml.bak`, one
layer up.

Measured before this guard existed: `ava_bridge/settings.py` (the only
implementation of the ava.yaml write path), `ava_bridge/hub/system.py` (whose
line 78 is `settings.save_patch({"code": {"approval": mode}})`) and
`overlay/ava_bridge/personal_access.py` (which mutates `_PROJECT_DENY` at
import) all classified `auto`. Inert under the shipped default `approval: all`,
because `code_agent.py` folds every auto edit into the approval bucket — and
live under `approval: policy`, a mode Setup → System offers.

WHAT THIS GUARD DOES NOT COVER, deliberately and on the record: `save_patch` is
importable, and six auto-tier modules already call it for unrelated keys. An
agent that adds `save_patch({"code": {"approval": "none"}})` to any of them
ungates itself without touching a gated file, and no glob list can enumerate
callers that do not exist yet. Closing that needs a key-level refusal inside
`settings.save_patch` — which is worth doing precisely because `settings.py` is
now approval-tier, so a check living there cannot be removed without the owner
seeing it. Tracked in the Phase 0 notes; this guard holds the line meanwhile.

Style follows tests/test_diagram_sync.py: a static scan over `git ls-files` that
runs anywhere and fails with the fix instruction. Note `overlay/**` is
gitignored, so it is not scanned here — the glob in `_APPROVAL` covers it, and a
scan cannot assert facts about files git does not track.
"""
import re

from gitfiles import ROOT, tracked

from ava_bridge import access_policy

# The config keys that decide what the agent may edit. Writing any of these is
# self-ungating, so the writer belongs in the approval or deny tier.
_GATE_KEYS = re.compile(
    r"""["']approval["']|["']approval_globs["']|["']deny_globs["']"""
    r"""|AVA_CODE_APPROVAL|AVA_CODE_DENY_GLOBS|AVA_CODE_APPROVAL_GLOBS"""
    r"""|_PROJECT_DENY"""
)
# A call that persists config to disk, as opposed to merely reading it.
_WRITE_CALL = re.compile(r"save_patch|save_config|_write_config")
# The functions that ARE the write path, wherever they are defined.
_WRITE_DEF = re.compile(r"^\s*def (save_patch|save_config|_write_config)\b", re.M)

_ALLOW = {
    # Names the pattern it bans, in the deny/approval lists and in comments.
    "ava_bridge/access_policy.py",
    # This file quotes the key names in order to scan for them.
    "tests/test_config_writers_gated.py",
}


def _read(rel: str) -> str:
    try:
        return (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _offenders(predicate) -> list[str]:
    out = []
    for rel in tracked("*.py"):
        if rel in _ALLOW or rel.startswith(("tests/", "qa/")):
            continue
        src = _read(rel)
        if predicate(src) and access_policy.classify(rel) == "auto":
            out.append(rel)
    return out


def test_gate_key_writers_are_not_auto() -> None:
    """A module that persists code.approval must need the owner's approval."""
    scanned = [r for r in tracked("*.py") if _WRITE_CALL.search(_read(r))]
    assert scanned, (
        "no module in the tree calls save_patch/save_config/_write_config — "
        "did the config write path get renamed? Update _WRITE_CALL."
    )

    offenders = _offenders(
        lambda src: bool(_GATE_KEYS.search(src) and _WRITE_CALL.search(src))
    )
    assert not offenders, (
        f"these modules write a self-ungating config key and classify 'auto', so "
        f"the agent can apply an edit to them with no owner approval: {offenders}. "
        "access_policy._DENY guards ava.yaml itself — 'writable = self-ungating' — "
        "but not the code that writes it. Add each module to access_policy._APPROVAL, "
        "or to _ALLOW in this file with a comment saying why it cannot ungate."
    )


def test_the_ava_yaml_write_path_is_not_auto() -> None:
    """Wherever save_patch/save_config lives, that module must be gated."""
    definers = [r for r in tracked("*.py") if _WRITE_DEF.search(_read(r))]
    assert definers, (
        "no module defines save_patch/save_config/_write_config — the ava.yaml "
        "write path moved. Update _WRITE_DEF so this guard keeps working."
    )

    offenders = [r for r in definers if access_policy.classify(r) == "auto"]
    assert not offenders, (
        f"these modules IMPLEMENT the ava.yaml write path and classify 'auto': "
        f"{offenders}. An agent edit here redirects or disarms every config write "
        "on the box, including code.approval, without the owner seeing a diff. "
        "Add them to access_policy._APPROVAL."
    )
