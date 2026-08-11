"""A fork must inherit no personality from this repo.

Standing decision: `agent/persona.txt.tmpl` carries ONLY operational directives —
the tool-calling mandates, the deny-by-default network correction, and identity.
How the assistant *talks* is the owner's, comes from `persona.style` /
`persona.format`, and is empty on a fresh install. Shipping a personality as if
it were a default makes every fork sound like whoever wrote the template, which
is the opposite of the product's whole claim.

Two halves, and both matter:

* NEGATIVE — the template must not regain a stylistic opinion. Written as a
  keyword scan because that is the failure mode: a well-meaning edit adds "be
  warm and friendly" back to the shared template instead of to a preset.
* POSITIVE — the operational mandates must still be there. Deleting one is
  silent and expensive: the repo has already shipped the refusal bug once
  (tests/test_tooling_note.py records "Ava once denied having web access because
  the sandbox preamble says outbound network is deny-by-default").

Before this module nothing in tests/ or qa/ rendered the persona at all, so an
empty, garbled, or brace-littered prompt shipped green. `agent/install.sh` only
logs a character count, and runtime/nemoclaw.py discards its stdout.
"""
import pathlib
import subprocess
import sys

import pytest

from gitfiles import require_git, tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE_REL = "agent/persona.txt.tmpl"

_FIX = ("Put it in a preset in ava_bridge/hub/persona.py (a starting point the "
        "owner can edit or ignore) or document it as an example of persona.style "
        "in config.example.yaml — never in the shared template, which every fork "
        "inherits verbatim.")

# Phrases that assert a TASTE in how the assistant speaks. Deliberately literal:
# each one is a clause that was in the template before the split, or an obvious
# way to reintroduce the same thing.
_STYLISTIC = (
    "spirit of siri", "be warm", "warm,", "texting a friend", "like a person texting",
    "close friend", "corporate assistant", "unfiltered opinion", "real opinions",
    "skip the hedging", "moral lecturing", "sanitized disclaimer",
    "mirror", "match your", "energy, slang", "profanity", "crude", "blunt talk",
    "don't force edginess", "conversational", "write like",
)

# Operational mandates that must survive any persona choice. (regex-free literal
# fragments, matched case-insensitively, paired with why they exist.)
_REQUIRED = (
    ("get_weather", "weather answers hallucinate without the tool-call mandate"),
    ("deny-by-default",
     "the sandbox preamble makes Ava deny having web access (see "
     "tests/test_tooling_note.py) — this is the correction"),
    ("as an AI", "refusal-deflection returns without the suppression clause"),
    ("tool_search_code", "the model tries to reach its own tools by writing code"),
)


def _template_text() -> str:
    require_git()
    if TEMPLATE_REL not in tracked(TEMPLATE_REL):
        pytest.skip(f"{TEMPLATE_REL} is not tracked")
    return (ROOT / TEMPLATE_REL).read_text(encoding="utf-8")


def test_the_template_carries_no_stylistic_opinion() -> None:
    text = _template_text().lower()
    found = sorted({p for p in _STYLISTIC if p in text})
    assert not found, (
        f"{TEMPLATE_REL} has regained a stylistic opinion: {found}. Every fork "
        f"inherits this file verbatim, so this is our taste becoming their "
        f"assistant's personality. {_FIX}"
    )


@pytest.mark.parametrize("fragment,why", _REQUIRED)
def test_the_operational_mandates_are_still_present(fragment: str, why: str) -> None:
    text = _template_text().lower()
    assert fragment.lower() in text, (
        f"{TEMPLATE_REL} no longer contains {fragment!r} — {why}. These directives "
        "are load-bearing for correctness, not character: they must stay "
        "unconditional in the template rather than moving into persona.style, "
        "which is empty on every fresh install."
    )


def _render(env: dict) -> str:
    """Render via a subprocess, the way agent/install.sh actually does it."""
    r = subprocess.run([sys.executable, str(ROOT / "agent" / "render_persona.py")],
                       capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, (
        "render_persona.py exited non-zero. agent/install.sh runs it under "
        f"`set -euo pipefail`, so this aborts provisioning mid-deploy:\n{r.stderr}")
    return r.stdout


def _base_env(tmp_path, **over) -> dict:
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith("AVA_PERSONA")}
    env["AVA_HOME"] = str(tmp_path)
    env.update(over)
    return env


def test_a_fresh_install_gets_no_style_at_all(tmp_path) -> None:
    """The headline guarantee. No ava.yaml, no env: no personality.

    Scans the RENDERED output, not just the template, because the template is not
    the only place taste can hide — the `chat` format block shipped with "keep
    replies conversational, lead with the answer" until this test was written, and
    a default-on format block is every bit as imposed as a default-on template.
    """
    out = _render(_base_env(tmp_path)).lower()
    leaked = sorted({p for p in _STYLISTIC if p in out})
    assert not leaked, (
        f"a fresh install's rendered persona carries stylistic opinion {leaked}. "
        f"persona.format may only describe what the surface can DISPLAY. {_FIX}")
    assert "style —" not in out, "a style block was emitted with no style set"
    assert "get_weather" in out, "the fresh persona lost its operational directives"


def test_every_placeholder_is_substituted(tmp_path) -> None:
    """An unsubstituted {{TOKEN}} reaches the model as literal braces, and nothing
    else in the stack notices: install.sh only logs a char count."""
    out = _render(_base_env(tmp_path))
    assert "{{" not in out and "}}" not in out, (
        f"unsubstituted placeholder(s) survived into the rendered persona:\n{out[:400]}")


def test_owner_style_cannot_splice_the_adult_clause(tmp_path) -> None:
    """A substituted value is re-scanned by every later .replace(), so style text
    containing the literal placeholder could switch on adult content that
    persona.adult keeps off. Both defences are asserted: braces are stripped, and
    the owner block is substituted last."""
    (tmp_path / "ava.yaml").write_text(
        'persona:\n  style: "terse. {{ADULT_BLOCK}}"\n  adult: false\n',
        encoding="utf-8")
    out = _render(_base_env(tmp_path))
    assert "{{" not in out, "brace pair survived from owner-supplied style text"
    assert "NSFW" not in out and "explicit" not in out.lower(), (
        "owner style text spliced the adult clause into a persona whose "
        "persona.adult is false — the single content gate was bypassed")


def test_a_yaml_bool_style_does_not_abort_provisioning(tmp_path) -> None:
    """`style: yes` is Python True, and True.strip() raises. Under install.sh's
    `set -euo pipefail` that aborts AFTER the MCP servers are already in the
    sandbox, leaving a half-provisioned runtime."""
    (tmp_path / "ava.yaml").write_text("persona:\n  style: yes\n", encoding="utf-8")
    out = _render(_base_env(tmp_path))
    assert "get_weather" in out, "a non-string style broke the render"
    assert "true" not in out.lower().split("formatting:")[0].replace("actually", ""), (
        "a YAML bool leaked into the prompt as text")


def test_an_unknown_format_falls_back_rather_than_crashing(tmp_path) -> None:
    (tmp_path / "ava.yaml").write_text("persona:\n  format: interpretive-dance\n",
                                       encoding="utf-8")
    out = _render(_base_env(tmp_path))
    assert "do NOT use markdown headings" in out, (
        "an unrecognised persona.format must fall back to the chat contract, "
        "because the chat UI renders plain text either way")


def test_the_chat_format_default_matches_the_renderer(tmp_path) -> None:
    """Not taste: frontend/src/components/chat/Message.tsx renders assistant text
    as a bare {text} in a pre-wrap bubble, so markdown would appear literally."""
    out = _render(_base_env(tmp_path))
    assert "do NOT use markdown headings" in out, (
        "the default persona dropped the no-markdown contract; a fresh install "
        "would render literal '## Heading' in the chat bubble")


def test_markdown_format_lifts_the_constraint(tmp_path) -> None:
    (tmp_path / "ava.yaml").write_text("persona:\n  format: markdown\n",
                                       encoding="utf-8")
    out = _render(_base_env(tmp_path))
    assert "rendered as markdown" in out
    assert "do NOT use markdown headings" not in out


def test_style_is_length_capped(tmp_path) -> None:
    """The persona crosses an argv/env boundary base64'd on the way to the
    sandbox; Linux caps a single argv string at 128 KiB (MAX_ARG_STRLEN) and
    base64 inflates 4/3, so an unbounded paste is an opaque E2BIG at provision."""
    import yaml
    (tmp_path / "ava.yaml").write_text(
        yaml.safe_dump({"persona": {"style": "x" * 99_999}}), encoding="utf-8")
    out = _render(_base_env(tmp_path))
    assert len(out) < 20_000, f"style was not capped; rendered {len(out)} chars"


def test_presets_are_offered_as_text_not_stored_as_ids() -> None:
    """Presets must be starting points, so that editing this list later cannot
    retroactively change how an existing install's assistant talks."""
    from ava_bridge.hub import persona as hub_persona
    assert hub_persona.PRESETS, "the panel offers no starting points"
    for p in hub_persona.PRESETS:
        assert {"id", "label", "text"} <= set(p), f"malformed preset: {p}"
        assert p["text"].strip(), f"preset {p['id']} has no text to hand the owner"
    ids = [p["id"] for p in hub_persona.PRESETS]
    assert len(ids) == len(set(ids)), f"duplicate preset ids: {ids}"


# ── The other four identity fields ───────────────────────────────────────────
# `persona.style` was guarded; `brand.name`, `owner.name`, `owner.location` and
# `owner.hardware` were not. Every hazard `clean_owner_text` documents applies to
# all five identically — only style had actually bitten someone, so only style
# got the guard.

def test_a_yaml_bool_in_any_identity_field_does_not_abort_provisioning(tmp_path) -> None:
    """Same failure as `style: yes`, four more ways in. Under install.sh's
    `set -euo pipefail` an AttributeError here aborts AFTER the MCP servers are
    already extracted into the sandbox."""
    for block, key in (("brand", "name"), ("owner", "name"),
                       ("owner", "location"), ("owner", "hardware")):
        (tmp_path / "ava.yaml").write_text(
            f"{block}:\n  {key}: yes\n", encoding="utf-8")
        out = _render(_base_env(tmp_path))
        assert "get_weather" in out, (
            f"a non-string {block}.{key} broke the render")


def test_an_identity_field_cannot_splice_a_template_block(tmp_path) -> None:
    """Owner text is substituted BEFORE the block placeholders are filled, so a
    `{{STYLE_BLOCK}}` in owner.name would be re-scanned and spliced — the same
    vector `test_owner_style_cannot_splice_the_adult_clause` closes for style."""
    (tmp_path / "ava.yaml").write_text(
        'owner:\n  name: "Bob {{ADULT_BLOCK}}{{STYLE_BLOCK}}"\n', encoding="utf-8")
    out = _render(_base_env(tmp_path))
    assert "{{" not in out, "brace pair survived from owner.name"
    assert "NSFW" not in out and "explicit" not in out.lower()


def test_identity_fields_are_length_capped(tmp_path) -> None:
    """The whole persona travels as one base64 argv positional. STYLE_MAX exists
    to keep it under MAX_ARG_STRLEN, and four fields bypassed it — so the opaque
    E2BIG it prevents was reachable through any of them."""
    (tmp_path / "ava.yaml").write_text(
        'owner:\n  location: "' + "x" * 50_000 + '"\n', encoding="utf-8")
    out = _render(_base_env(tmp_path))
    assert len(out) < 20_000, "an identity field is still unbounded"


def test_an_empty_identity_field_still_falls_back_to_its_default(tmp_path) -> None:
    (tmp_path / "ava.yaml").write_text('brand:\n  name: ""\n', encoding="utf-8")
    assert "Ava" in _render(_base_env(tmp_path))
