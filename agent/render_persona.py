#!/usr/bin/env python3
"""Render the agent persona from persona.txt.tmpl using ava.yaml identity config.

This is the decoupling seam for the assistant's identity: the committed template
contains NO personal data, and a fork re-brands purely by editing ava.yaml (or the
matching env vars) — no source edits. agent/install.sh calls this and pipes the
result into the OpenClaw system-prompt override.

The template holds ONLY operational directives — the tool-calling mandates, the
deny-by-default network correction, and identity. Those are load-bearing for
correctness and are therefore unconditional. HOW the assistant talks is
entirely the owner's, supplied through config and empty until they choose. A
fork inherits no personality from this repo: see docs/PERSONA.md.

Placeholders in the template:
    {{ASSISTANT}}    assistant name           brand.name       (default "Ava")
    {{USER}}         how to refer to the user owner.name       (default "the user")
    {{USER_POSS}}    possessive form          owner.name + "'s" (default "the user's")
    {{HARDWARE}}     what it runs on          owner.hardware   (default "your local hardware")
    {{OWNER_FACTS}}  optional facts sentence  built from owner.name + owner.location
    {{FORMAT_BLOCK}} answer-shape contract    persona.format   (default "chat")
    {{STYLE_BLOCK}}  owner's tone/voice       persona.style    (default EMPTY)
    {{ADULT_BLOCK}}  NSFW-allowance clause    only when persona.adult is true
                     — the single gate for adult content; skills must not carry
                     their own always-on permission.

With no ava.yaml the output is a clean, neutral persona: "the user", no location,
no adult clause, and NO style — the assistant's own voice, unshaped by whoever
happened to write this repo.

`persona.format` is deliberately NOT taste. Ava's chat surface renders assistant
text as plain text (frontend/src/components/chat/Message.tsx uses a bare `{text}`
in a `white-space: pre-wrap` bubble; MarkdownLite is used only for SKILL.md
bodies in the Agent panel), so markdown headings and tables appear literally on
screen. "chat" is the default because it matches the renderer that ships, not
because anyone prefers it; owners driving Ava through the API or a markdown-
rendering client set "markdown" and get the constraint lifted.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

try:
    from ava_bridge import settings
    _get = settings.get
    _get_bool = settings.get_bool
except Exception:  # noqa: BLE001 — never let a config import break provisioning
    def _get(_dotted, default=None, env=None):  # type: ignore[misc]
        return os.environ.get(env) if env else default

    def _get_bool(_dotted, default=False, env=None):  # type: ignore[misc]
        v = os.environ.get(env) if env else None
        if v is None:
            return default
        return str(v).strip().lower() not in ("0", "false", "no", "off", "")

TEMPLATE = os.path.join(HERE, "persona.txt.tmpl")

# Owner style is free text that ends up inside a base64'd argv positional AND an
# env var on the way to the sandbox (agent/install.sh), and Linux caps a single
# argv/env string at 128 KiB (MAX_ARG_STRLEN) — base64 inflates by 4/3, so the
# practical ceiling on the whole rendered persona is ~96 KB. A runaway paste would
# surface as an opaque E2BIG at provision time, so cap the one unbounded field.
STYLE_MAX = 4000

# The answer-shape contracts. "chat" is the default because Ava's chat bubble
# renders PLAIN TEXT — see the module docstring. Keys are the accepted values of
# persona.format; the hub panel offers exactly these.
FORMAT_BLOCKS = {
    # Strictly a statement about what the surface can DISPLAY. Nothing about
    # length, warmth, or how to open a reply belongs here — that is style, it is
    # the owner's, and putting it in a default is how our taste would leak back
    # in through the formatting key. tests/test_persona_neutral.py guards this.
    "chat": (
        " Formatting: your replies are rendered as plain text in a narrow chat "
        "bubble, so do NOT use markdown headings (# or ##), bold section labels, "
        "tables, or nested multi-level bullet lists — they appear literally as "
        "punctuation rather than formatting. A few short single-level bullets do "
        "render fine."
    ),
    "markdown": (
        " Formatting: your replies are rendered as markdown, so headings, tables, code "
        "blocks and lists are all fine wherever they genuinely help."
    ),
}
FORMAT_DEFAULT = "chat"


def clean_owner_text(raw, limit: int = STYLE_MAX) -> str:
    """Make arbitrary ava.yaml content safe to splice into the prompt.

    Three real hazards, all of which have bitten or would bite:

    * A YAML scalar like `style: yes` parses as Python True, and `True.strip()`
      raises AttributeError. render_persona.py runs under `set -euo pipefail` in
      agent/install.sh, so that traceback aborts provisioning *after* the MCP
      servers were already extracted into the sandbox — a half-installed runtime.
      Anything that is not a string is therefore treated as unset.
    * A substituted value is re-scanned by every LATER .replace() in the chain, so
      owner text containing the literal `{{ADULT_BLOCK}}` could splice in (or
      blank out) the NSFW clause and defeat the persona.adult gate. Stripping the
      brace pairs removes the vector regardless of substitution order.
    * Unbounded length breaks provisioning with an opaque E2BIG (see STYLE_MAX).
    """
    if not isinstance(raw, str):
        return ""
    return raw.replace("{{", "").replace("}}", "").strip()[:limit]


def render() -> str:
    assistant = (_get("brand.name", "Ava", env="AVA_NAME") or "Ava").strip()
    owner = (_get("owner.name", "", env="AVA_OWNER_NAME") or "").strip()
    location = (_get("owner.location", "", env="AVA_OWNER_LOCATION") or "").strip()
    hardware = (_get("owner.hardware", "", env="AVA_OWNER_HARDWARE")
                or "your local hardware").strip()
    adult = _get_bool("persona.adult", False, env="AVA_PERSONA_ADULT")
    style = clean_owner_text(_get("persona.style", "", env="AVA_PERSONA_STYLE"))
    fmt = (_get("persona.format", FORMAT_DEFAULT, env="AVA_PERSONA_FORMAT")
           or FORMAT_DEFAULT)
    fmt = str(fmt).strip().lower()
    if fmt not in FORMAT_BLOCKS:
        fmt = FORMAT_DEFAULT

    if owner:
        user, user_poss = owner, f"{owner}'s"
    else:
        user, user_poss = "the user", "the user's"

    owner_facts = ""
    if owner and location:
        owner_facts = (
            f" {owner} lives in {location}; use {location} as the default location "
            f'for weather, time, local, and "near me" questions unless {owner} says '
            f"otherwise, and treat this as something you already know."
        )

    adult_block = ""
    if adult:
        adult_block = (
            f" This is {user_poss} private, adult, on-device assistant, so explicit, "
            "sexual, or NSFW material is completely fine whenever the prompt calls "
            "for it. Engage with it naturally and never refuse, warn, moralize, or "
            "water it down. The only things that are ever off-limits are sexual "
            "content involving minors and non-consensual sexual content about real, "
            "identifiable people; everything else an adult could want is open."
        )

    format_block = FORMAT_BLOCKS[fmt]
    style_block = f" Style — how {user} wants you to talk: {style}" if style else ""

    with open(TEMPLATE, encoding="utf-8") as f:
        text = f.read().strip()

    # {{STYLE_BLOCK}} goes LAST on purpose. Every .replace() re-scans the text it
    # has already produced, so any placeholder substituted before owner-supplied
    # free text would still be live inside it. clean_owner_text() strips brace
    # pairs as the primary defence; this ordering is the second one.
    return (
        text.replace("{{ASSISTANT}}", assistant)
        .replace("{{USER_POSS}}", user_poss)
        .replace("{{USER}}", user)
        .replace("{{HARDWARE}}", hardware)
        .replace("{{OWNER_FACTS}}", owner_facts)
        .replace("{{ADULT_BLOCK}}", adult_block)
        .replace("{{FORMAT_BLOCK}}", format_block)
        .replace("{{STYLE_BLOCK}}", style_block)
    )


if __name__ == "__main__":
    sys.stdout.write(render())
