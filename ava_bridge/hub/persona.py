"""Setup -> Agent -> Persona panel: how Ava talks, and who decides.

A fork of this repo must inherit NO personality from whoever wrote it. The prompt
template (agent/persona.txt.tmpl) therefore carries only operational directives —
the tool-calling mandates, the deny-by-default network correction — and the
whole of the assistant's voice comes from here, empty until the owner sets it.

Kept out of features.REGISTRY on purpose: that registry is for optional
capabilities with a service behind them that can be off or unreachable, and its
preflight() codes describe nothing about a style string. These are plain config,
like owner.name.

Two things worth knowing about the write path:

* Nothing here changes how Ava speaks until the agent runtime is re-provisioned.
  The system prompt is rendered ONCE, by agent/render_persona.py during
  agent/install.sh, and base64'd into the runtime's config. So the response says
  `reprovision_required` and the panel points at Setup -> Agent. A reload of the
  chat page will not do it.
* An `AVA_PERSONA_*` environment variable outranks ava.yaml, so a save can appear
  to succeed and change nothing. The GET reports any such override and the panel
  shows it, rather than letting the owner wonder.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import settings

router = APIRouter()

# Presets are STARTING POINTS offered by the UI, never stored as an id. The panel
# drops the text into the editable field and the owner saves whatever they end up
# with, so a later edit to this list can never retroactively change how somebody
# else's assistant talks. That is the whole point: no opinion of ours reaches an
# install after it is set up.
PRESETS = [
    {"id": "concise", "label": "Concise",
     "text": "Answer directly and stop. Lead with the answer, keep it short, and skip "
             "preambles, restatements of the question, and wrap-up sign-offs."},
    {"id": "warm", "label": "Warm",
     "text": "Be friendly and conversational. Talk like a helpful person rather than a "
             "manual, but do not pad the answer to get there."},
    {"id": "professional", "label": "Professional",
     "text": "Be clear, neutral, and well organised. Structure longer answers so they "
             "are easy to scan, and prefer precise wording over casual phrasing."},
    {"id": "blunt", "label": "Blunt",
     "text": "Be direct and frank. Say what you actually think, skip hedging and "
             "unnecessary caveats, and do not soften a real problem into a suggestion."},
]

FORMAT_CHOICES = [
    {"id": "chat", "label": "Chat bubble",
     "hint": "Plain text, no markdown. Matches Ava's own chat UI, which renders "
             "assistant replies as plain text — headings and tables would show up "
             "literally."},
    {"id": "markdown", "label": "Markdown",
     "hint": "Allows headings, tables, code blocks and lists. For clients that render "
             "markdown, or when you drive Ava through the API."},
]


@router.get("/persona")
def persona_get():
    """Current persona config plus the presets the panel offers as starting points."""
    return {
        "style": settings.persona_style(),
        "format": settings.persona_format(),
        "adult": settings.get_bool("persona.adult", False, env="AVA_PERSONA_ADULT"),
        "presets": PRESETS,
        "format_choices": FORMAT_CHOICES,
        "style_max": 4000,
        # Truthy here means ava.yaml is being outranked and a save will not bite.
        "env_overrides": {k: v for k, v in {
            "style": settings.env_override("AVA_PERSONA_STYLE"),
            "format": settings.env_override("AVA_PERSONA_FORMAT"),
            "adult": settings.env_override("AVA_PERSONA_ADULT"),
        }.items() if v is not None},
        "config_error": settings.load_error(),
    }


@router.post("/persona")
async def persona_set(request: Request):
    """Persist persona.* to ava.yaml — no source edits.

    Presence-checked, not truthiness-checked: an empty style is a legitimate value
    meaning "clear it and give me the model's own voice", and `if body.get("style")`
    would silently refuse to honour that.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a malformed body is a 400, not a 500
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "expected a JSON object"},
                            status_code=400)

    patch: dict = {}
    if "style" in body:
        raw = body["style"]
        if raw is None:
            patch["style"] = ""
        elif isinstance(raw, str):
            # Brace pairs are stripped because a substituted value is re-scanned by
            # the later substitutions in render_persona.render(); leaving them in
            # would let style text reach the adult-content clause.
            cleaned = raw.replace("{{", "").replace("}}", "").strip()
            if len(cleaned) > 4000:
                return JSONResponse(
                    {"ok": False, "error": "style must be 4000 characters or fewer"},
                    status_code=400)
            patch["style"] = cleaned
        else:
            return JSONResponse({"ok": False, "error": "style must be text"},
                                status_code=400)

    if "format" in body:
        fmt = str(body["format"] or "").strip().lower()
        if fmt not in settings.PERSONA_FORMATS:
            return JSONResponse(
                {"ok": False,
                 "error": f"format must be one of {', '.join(settings.PERSONA_FORMATS)}"},
                status_code=400)
        patch["format"] = fmt

    if not patch:
        return JSONResponse({"ok": False, "error": "nothing to set"}, status_code=400)

    try:
        settings.save_patch({"persona": patch})
    except settings.ConfigParseError as e:
        # Must precede the broad handler: ConfigParseError subclasses RuntimeError,
        # so catching Exception first would turn a recoverable "your ava.yaml does
        # not parse" into an opaque 500.
        return JSONResponse({"ok": False, "error": str(e),
                             "error_code": "config_unparseable"}, status_code=409)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"could not write ava.yaml: {e}"},
                            status_code=500)
    return {"ok": True, "reprovision_required": True}
