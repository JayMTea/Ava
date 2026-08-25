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
    {{APPS_BLOCK}}   the owner's connected apps  derived at render time from the
                     connector manifests (ava_bridge.connectors.agent_surface)
                     and the tool names each app reported on its last discovery
                     (ava_bridge.tools_cache): one line per app saying how it is
                     reached and what it offers, so the model routes "what did I
                     eat today" to the right app instead of guessing. EMPTY when
                     no app connector exposes tools, so a fresh fork's persona is
                     byte-for-byte what it was before the placeholder existed.
                     Nothing about anyone's apps is in the tracked template.

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

# The answer-shape contracts. "markdown" is the default because Ava's chat
# bubble now renders markdown (frontend/src/components/chat/Message.tsx renders
# the reply through MarkdownLite — tables, code, links, bold — and shows agent
# media as players). Keys are the accepted values of persona.format; the hub
# panel offers exactly these. An owner who wants plain text sets "chat".
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
FORMAT_DEFAULT = "markdown"


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


#: The connected-apps block is bounded twice: names per app, and the whole block.
#: It travels inside the same base64'd argv positional as everything else (see
#: STYLE_MAX), and an owner with many apps or one app with hundreds of tools must
#: not turn provisioning into an E2BIG. 25 names is enough to route on; past
#: that the model is told to search with the app's find_tool, which is the path
#: it has to take anyway to learn an action's input schema.
APPS_TOOLS_MAX = 25
APPS_MAX = 6000


def _app_surface() -> list:
    """The connected apps the agent can reach, or [] when that cannot be known.

    Wrapped because this runs under agent/install.sh's `set -euo pipefail`: a
    manifest that fails to load, a missing pyyaml, or a cache file that will not
    parse must cost the persona its apps line, never the whole provision.
    """
    try:
        from ava_bridge import connectors
        return list(connectors.agent_surface())
    except Exception as e:  # noqa: BLE001 — see above
        sys.stderr.write(f"[render_persona] connected-apps block skipped: {e}\n")
        return []


def _name(raw, limit: int = 64) -> str:
    """A manifest label or tool name, made safe to splice (see clean_owner_text)
    and flattened to one line — a label with a newline would break the
    one-paragraph persona."""
    return " ".join(clean_owner_text(raw, limit=limit).split())


def apps_block(apps: list, user: str = "the user",
               user_poss: str = "the user's") -> str:
    """Render the connected-apps sentence(s) from `connectors.agent_surface()`
    rows. Pure: no I/O, so it is unit-testable with hand-built rows.

    One compact clause per app — label, connector id, HOW it is reached (native
    `<id>_<action>` tools, or the `<id>_find_tool` -> `<id>_call` pair), and up
    to APPS_TOOLS_MAX tool names — then one operational directive that holds for
    every app. Nothing here is style: it is the same class of mandate as "call
    get_weather for weather", extended to the apps the owner wired in. Returns
    "" when there are no apps, so the placeholder vanishes without a trace.
    """
    entries: list[str] = []
    omitted = 0
    for app in apps:
        if not isinstance(app, dict):
            continue
        cid = _name(app.get("id"))
        label = _name(app.get("label")) or cid
        if not cid:
            continue
        names = [n for n in (_name(t) for t in (app.get("tools") or [])) if n]
        shown, more = names[:APPS_TOOLS_MAX], max(0, len(names) - APPS_TOOLS_MAX)
        if app.get("meta"):
            head = (f"{label} (connector id {cid}): search its actions with "
                    f"{cid}_find_tool, then run one by its exact name with {cid}_call")
            if shown:
                tail = f"; known actions: {', '.join(shown)}"
                if more:
                    tail += f" (and {more} more — search for them)"
            else:
                tail = "; its actions are discovered live, so search first"
            entry = head + tail + "."
        else:
            tools = [f"{cid}_{n}" for n in shown]
            entry = (f"{label} (connector id {cid}): native tool calls "
                     f"{', '.join(tools)}" if tools else
                     f"{label} (connector id {cid}): native tools named {cid}_<action>")
            if more:
                entry += f" (and {more} more)"
            entry += "."
        # Whole-block cap, at an app boundary: a truncated tool name would be a
        # name that does not exist, which is exactly what this block is for
        # preventing. The count of what was cut is said out loud instead.
        if sum(len(e) + 1 for e in entries) + len(entry) > APPS_MAX:
            omitted += 1
            continue
        entries.append(entry)
    if not entries and not omitted:
        return ""
    intro = (f" Connected apps: {user_poss} own apps are wired in as tools; for "
             f"anything about them, use the app named here rather than guessing.")
    body = " " + " ".join(entries) if entries else ""
    if omitted:
        body += (f" {omitted} more app(s) are connected; each has its own "
                 f"<id>_find_tool and <id>_call tools.")
    directive = (
        " For a find_tool/call pair, always search first and then call the exact "
        "name it returned with arguments matching that action's inputSchema. If a "
        f"call answers with a consent or approval prompt, tell {user} and wait for "
        "their decision — never retry the same call in a loop. Never fabricate or "
        "guess app data: answer from the tool's result, and if a tool fails, say so."
    )
    return intro + body + directive


#: The other four free-text identity fields. They are shorter than `style` in
#: practice, but nothing enforced that — and every hazard `clean_owner_text`
#: documents applies to them identically. A `brand:\n  name: yes` reached
#: `.strip()` on a bool and aborted provisioning mid-install; `owner.name` could
#: splice `{{STYLE_BLOCK}}` because substitution happens BEFORE the block
#: placeholders are filled; and none of them was length-capped, so the E2BIG
#: `STYLE_MAX` exists to prevent was reachable through any of the four. Only
#: `persona.style` was guarded, and only because it was the one somebody had
#: already been bitten by.
IDENTITY_MAX = 400


def render() -> str:
    def _ident(key: str, env: str, default: str = "") -> str:
        return clean_owner_text(_get(key, default, env=env), limit=IDENTITY_MAX) \
            or default

    assistant = _ident("brand.name", "AVA_NAME", "Ava")
    owner = _ident("owner.name", "AVA_OWNER_NAME")
    location = _ident("owner.location", "AVA_OWNER_LOCATION")
    hardware = _ident("owner.hardware", "AVA_OWNER_HARDWARE", "your local hardware")
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
    # Derived, not configured: `_app_surface()` is [] on a fork with no apps,
    # and apps_block([]) is "", so the placeholder leaves no trace.
    apps = apps_block(_app_surface(), user=user, user_poss=user_poss)

    with open(TEMPLATE, encoding="utf-8") as f:
        text = f.read().strip()

    # {{STYLE_BLOCK}} goes LAST on purpose. Every .replace() re-scans the text it
    # has already produced, so any placeholder substituted before owner-supplied
    # free text would still be live inside it. clean_owner_text() strips brace
    # pairs as the primary defence; this ordering is the second one.
    # {{APPS_BLOCK}} is manifest-derived text (labels, tool names) and gets the
    # same treatment: brace-stripped by `_name`, and substituted after every
    # block it could otherwise splice.
    return (
        text.replace("{{ASSISTANT}}", assistant)
        .replace("{{USER_POSS}}", user_poss)
        .replace("{{USER}}", user)
        .replace("{{HARDWARE}}", hardware)
        .replace("{{OWNER_FACTS}}", owner_facts)
        .replace("{{ADULT_BLOCK}}", adult_block)
        .replace("{{FORMAT_BLOCK}}", format_block)
        .replace("{{APPS_BLOCK}}", apps)
        .replace("{{STYLE_BLOCK}}", style_block)
    )


if __name__ == "__main__":
    sys.stdout.write(render())
