#!/usr/bin/env python3
"""Render the agent persona from persona.txt.tmpl using ava.yaml identity config.

This is the decoupling seam for the assistant's identity: the committed template
contains NO personal data, and a fork re-brands purely by editing ava.yaml (or the
matching env vars) — no source edits. agent/install.sh calls this and pipes the
result into the OpenClaw system-prompt override.

Placeholders in the template:
    {{ASSISTANT}}    assistant name           brand.name       (default "Ava")
    {{USER}}         how to refer to the user owner.name       (default "the user")
    {{USER_POSS}}    possessive form          owner.name + "'s" (default "the user's")
    {{HARDWARE}}     what it runs on          owner.hardware   (default "your local hardware")
    {{OWNER_FACTS}}  optional facts sentence  built from owner.name + owner.location
    {{ADULT_BLOCK}}  NSFW-allowance clause    only when persona.adult is true

With no ava.yaml the output is a clean, neutral persona ("the user", no default
location, no adult clause) so a fresh fork works out of the box.
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


def render() -> str:
    assistant = (_get("brand.name", "Ava", env="AVA_NAME") or "Ava").strip()
    owner = (_get("owner.name", "", env="AVA_OWNER_NAME") or "").strip()
    location = (_get("owner.location", "", env="AVA_OWNER_LOCATION") or "").strip()
    hardware = (_get("owner.hardware", "", env="AVA_OWNER_HARDWARE")
                or "your local hardware").strip()
    adult = _get_bool("persona.adult", False, env="AVA_PERSONA_ADULT")

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
            "sexual, or NSFW conversation is completely fine whenever the prompt calls "
            "for it — engage with it naturally and never refuse, warn, moralize, or "
            "water it down. The only things that are ever off-limits are sexual content "
            "involving minors and non-consensual sexual content about real, identifiable "
            "people; everything else an adult could want is open."
        )

    with open(TEMPLATE, encoding="utf-8") as f:
        text = f.read().strip()

    return (
        text.replace("{{ASSISTANT}}", assistant)
        .replace("{{USER_POSS}}", user_poss)
        .replace("{{USER}}", user)
        .replace("{{HARDWARE}}", hardware)
        .replace("{{OWNER_FACTS}}", owner_facts)
        .replace("{{ADULT_BLOCK}}", adult_block)
    )


if __name__ == "__main__":
    sys.stdout.write(render())
