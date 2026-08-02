# Branding

Ava ships one look. This is how you replace it with yours — the name it answers
to, its colour, and its logo.

**The app icon is not brandable, deliberately.** The browser tab, the
home-screen tile and the maskable icon are Ava's on every install, however the
install is named. Branding changes what the app looks like *to the person signed
into it*; it does not change what the product identifies itself as. The sign-in
card follows the same rule and shows Ava's mark. Everything else below is
yours.

**None of it costs anything.** Re-branding software you self-host is the premise
of self-hosting it, not an upsell, and that is enforced rather than promised:
`tests/test_no_capability_gate.py` fails the build if any tracked file ever
conditions a capability on a licence, plan or subscription value. If you find one,
it is a bug.

Everything below is written to `$AVA_HOME/ava.yaml` and `$AVA_HOME/branding/`.
No source is edited, and **no restart is needed** — a save is visible on the next
request.

---

## The quick version

**Setup → Branding.** Pick a colour, type a name, drop in a logo, press Save.

Every field is blank by default, and blank means Ava's shipped look **byte for
byte** — not "unset, fall back to something". An install that never opens this
page renders from exactly the values it always did, because the CSS rules that
apply a custom brand simply never match. "Reset to Ava's defaults" is an ordinary
save of empty strings, not a special path.

---

## What you can change

| | Config key | What it reaches |
|---|---|---|
| Name | `brand.name` | header, drawer, browser tab, sign-in page, PWA label, and the assistant's own sense of what it is called |
| Tagline | `brand.tagline` | sign-in card, installed-app description |
| Accent | `brand.accent` | **everything** — see below |
| Light accent | `brand.accent_light` | optional; blank uses `accent` in both themes |
| Chrome | `brand.chrome` | the browser/phone status bar around the app, and the PWA splash |
| Logo | `brand.logo` | the square mark inside the signed-in app |
| Wordmark | `brand.wordmark` | sidebar head; blank renders the name as text, which is how Ava ships |

### One colour, not eight

You set `accent`. Hover, focus rings, the faint chip fills, the active-pill text
and border, and chart series 1 are all **derived** from it, in both light and
dark, by `color-mix()` in the stylesheet. There is nothing else to pick and
nothing that can drift out of step with your choice.

### Ava will refuse an unreadable colour

The accent is used two ways at once — as a **background under white text** (about
fifteen places, including your own chat bubble) and as a **mark on the canvas**.
Those pull in opposite directions, so the thresholds are asymmetric:

| Check | Bar | Why |
|---|---|---|
| white text on your accent | 4.5:1 | WCAG 1.4.3 AA, body text |
| accent on the dark canvas | 3.0:1 | WCAG 1.4.11, non-text |
| accent on the light canvas | 3.0:1 | same |

For calibration, Ava's own `#007acc` scores **4.51** on white and **3.36** on the
dark canvas — it passes the text bar by 0.01 and does *not* clear 4.5 against the
canvas. A guard applying 4.5:1 to both would reject the colour Ava itself ships.

A refusal always comes with a suggestion: the nearest lightness of **the same
hue** that passes, walked in OKLab so your colour comes back recognisably your
colour rather than shifted. Set `brand.accessibility_check: false` to override —
deliberately, and knowing that light accents (yellow, lime) are the ones that
fail, because white text cannot sit on them.

### Images

PNG, WebP or JPEG, between 64px and 4096px, under 2 MB.

**SVG is refused.** Ava serves assets from its own origin and sets no
Content-Security-Policy, so an uploaded SVG would execute its own `<script>` with
your session cookie. That is a real hole, not a hypothetical one, and refusing
the format closes it more reliably than sanitising it would.

Every upload is **re-encoded through Pillow**. What gets stored is a PNG this
server produced, never the bytes you sent, which means EXIF, appended payloads
and format trickery stop existing rather than needing to be detected.

There is no icon slot. The favicon, the PWA 192/512, the maskable 512 and the
apple-touch 180 are Ava's shipped files and are served unconditionally — nothing
is rendered from an upload. Earlier versions derived that set from a `brand.icon`
you uploaded, falling back to `brand.logo`, which meant uploading a logo silently
re-branded your browser tab. Both paths are gone; `tests/test_brand_icon_is_ava.py`
fails if either returns.

---

## Brand packs — `ava-brand/1`

A pack is one zip holding a brand. Export yours from Setup → Branding, or write
one; the format is documented here **so that you can**, and a pack from any
generator loads the same way.

```
brand.zip
├── MANIFEST.json    format, generator, and a sha256 for every file
├── brand.yaml       ONLY a `brand:` block
├── logo.png
├── logo_light.png
├── wordmark.png
└── wordmark_light.png
```

`MANIFEST.json`:

```json
{
  "format": "ava-brand/1",
  "created": 1753900000.0,
  "generator": "your-tool 1.0",
  "name": "Northwind",
  "files": [
    { "name": "logo.png", "sha256": "…", "bytes": 41233, "w": 512, "h": 512,
      "type": "image/png" }
  ]
}
```

`brand.yaml` may set exactly five keys — `name`, `tagline`, `accent`,
`accent_light`, `chrome`. Anything else is ignored.

### What a pack cannot do

A pack arrives from someone who is not you, so it is the least trusted input in
the system and is treated that way:

- **Its keys are allowlisted, never merged.** A pack cannot set
  `server.trusted_hosts`, `code.approval`, `paths.*`, or anything else outside
  `brand.*`. Merging a pack's document would make a logo file a remote-code-
  execution vector; this is the single most important rule in the format.
- **Every image is re-validated** by the same code path a manual upload uses.
  There is no "trusted because it came from a pack" shortcut.
- **The zip is checked before it is read**: entry names (no absolute paths, no
  `..`), at most 16 entries, 2 MB per file, 12 MB total, and a compression-ratio
  ceiling that refuses a zip bomb.
- **Checksums are verified before anything is ingested**, and a mismatch is a
  refusal, not a warning.
- **An unknown `format` is refused by name**, so the version is a real contract.
- **It applies all-or-nothing.** A pack that fails on its fourth image leaves
  your existing brand untouched.

### Packs are never signature-gated

You may sign a pack, and a catalogue may use that to say where one came from.
**Ava does not check it.** The loader accepts any well-formed pack, unsigned
included, and `tests/test_no_capability_gate.py` asserts the loader contains no
signature verification at all.

This is deliberate. The moment an app verifies a signature to decide whether to
*load* something, the obvious next step is refusing what did not come from an
approved source — and a self-hosted app that refuses your own files is not yours.
Provenance is a label. It must never become a permission.

---

## Turning branding off

`features.branding` (env `AVA_BRANDING`) controls whether **this install may edit
its own brand**. It is on by default.

It gates writing, not rendering. With it off, an instance still *looks* like
whatever brand it was given — it just cannot be changed from inside. That
asymmetry is what lets someone who provisions instances for other people hand out
a branded Ava with the brand pinned, using nothing but the environment the
instance is launched with:

```
AVA_NAME=Northwind AVA_BRAND_ACCENT=#2f7d4f AVA_BRANDING=0
```

Environment variables outrank `ava.yaml`, so the values hold and the Setup panel
reports itself read-only rather than letting a save fail silently. If you are
using an Ava someone else set up and Branding is greyed out, that is why.

---

## Where things live

```
$AVA_HOME/ava.yaml            the brand: block
$AVA_HOME/branding/           uploaded images, content-addressed
$AVA_HOME/branding/derived/   legacy; only on installs upgraded from a version
                              that rendered branded icons. Cleared whenever
                              branding is touched. Safe to delete.
```

`branding/` is deliberately **not** `brand/`: `AVA_HOME` defaults to the code
root, where `brand/` is already a gitignored folder of logo source art, and
"reset to defaults" clears this directory.

The agent cannot write here — `branding/**` is in `access_policy._DENY`, because
your artwork is not Ava's to rewrite.

---

## Changing the name changes one more thing

The agent bakes its own name into its system prompt when it is provisioned, so a
**name** change asks you to re-provision under Setup → Agent before the assistant
refers to itself the new way. Colours and images never touch the agent, so they
need nothing.
