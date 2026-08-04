# Persona - your assistant's voice is yours

A **persona** decides how your assistant talks: how long its answers run, how
warm or blunt it is, whether it hedges, whether it uses markdown. You write it as
a sentence, and every reply follows it from then on.

Ava ships with **no personality**, deliberately. If you fork or install Ava, you
do not inherit the maintainer's taste in how an assistant should talk - you get
the model's own voice until you shape it.

## What actually ships

The prompt Ava starts from (`agent/persona.txt.tmpl`) contains only
**operational** directives - the things that make it *work*:

- Call `get_weather` for anything about weather, rather than answering from
  training data.
- Reach its own tools as native tool calls, not by writing code.
- Ignore the sandbox's "outbound network is deny-by-default" notice when it comes
  to its own built-in tools - otherwise the model concludes it has no web access
  and says so, which it has done before.
- Its name, who it serves, and what hardware it runs on, all from your config.

There is nothing in there about being warm, being concise, texting like a friend,
mirroring your slang, avoiding hedging, or having opinions. Earlier versions did
carry all of that. It was one person's preference shipping as everyone's default,
so it moved out.

## Setting your own

**Setup → Agent → Persona**, or directly in `ava.yaml`:

```yaml
persona:
  style: "dry, understated, never gushes"
  format: chat
  adult: false
```

| Key | Default | What it does |
| --- | --- | --- |
| `style` | *empty* | Free text, written straight into the prompt. Empty means the model's own voice, unshaped. Max 4000 characters. |
| `format` | `chat` | `chat` = plain text, no markdown. `markdown` = headings, tables and code blocks allowed. |
| `adult` | `false` | The single gate for explicit content in conversation. |

Environment overrides: `AVA_PERSONA_STYLE`, `AVA_PERSONA_FORMAT`,
`AVA_PERSONA_ADULT`. These outrank `ava.yaml`, so if one is set the Setup panel
will tell you that saving there won't take effect.

### Write it as an instruction

`style` is prompt text, so phrase it the way you would phrase a brief: *"dry,
understated, never gushes"* works; *"be nicer"* does not give the model much to
act on. The Setup panel offers four starting points (Concise, Warm, Professional,
Blunt) - picking one **drops its text into an editable box**. What gets saved is
the text, not the preset name, so a later change to those presets upstream can
never reach back and alter how your assistant talks.

!!! note "Why `format` defaults to `chat`"

    Not taste - a renderer fact. Ava's own chat surface displays assistant
    replies as plain text (`frontend/src/components/chat/Message.tsx` renders a
    bare `{text}` in a `white-space: pre-wrap` bubble). Markdown headings and
    tables would appear literally, as `##` and pipe characters. If you drive Ava
    through the API, or from a client that renders markdown, set
    `format: markdown` and the constraint lifts.

## It takes effect on the next provision

The system prompt is built **once**, when the agent runtime is **provisioned**
(rebuilt and loaded into its sandbox: `agent/install.sh` runs
`agent/render_persona.py` and hands the result to the runtime). Saving a new
persona does not change a conversation already in flight, and reloading the page
will not do it either. Re-provision from **Setup → Agent** afterwards.

??? note "For forkers: what keeps the shipped template neutral"

    - Nothing in `ava.yaml` is tracked (`.gitignore` covers `ava.yaml*`), so
      your persona never lands in a commit or a pull request.
    - The template is in the approval tier of the self-editing policy
      (`ava_bridge/access_policy.py`), so Ava cannot quietly rewrite its own
      operational mandates - a change there waits for you regardless of your
      `code.approval` setting.
    - `tests/test_persona_neutral.py` fails if the shipped template ever regains
      a stylistic opinion, and separately if it loses an operational one. If you
      are adding character, add it as a preset or as your own `persona.style` -
      not to the template every fork inherits.

---

**Next:** make it look the part too - [Branding](BRANDING.md).
