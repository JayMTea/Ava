# 0004. Use TALA as the diagram layout engine

- **Status:** Superseded (historical) — reverted to ELK. Unlicensed TALA renders
  stamp an unlicensed-copy watermark across the output (three tracked diagrams
  shipped watermarked; `tests/test_no_owner_identity.py` now fails the build on
  that watermark text), so
  `diagram_style.d2.layout` is `elk`. Kept for the decision history.
- **Date:** 2026-06-29
- **Deciders:** project owner

## Context

The generated diagrams are rendered with D2. The default `dagre` engine produces
curved spline edges; ELK produces orthogonal (right-angle) routing. As the diagram
set grew (system, network, policy, security), we wanted a layout tuned for software
architecture diagrams with clean, consistent routing across all current and future
diagrams.

## Decision

Use **TALA** (Terrastruct's proprietary layout engine, `d2plugin-tala`) as the
layout engine for all generated diagrams. The choice is a single manifest token —
`diagram_style.d2.layout: tala` — so every diagram (and every future one) inherits
it automatically. The API token lives in `~/.config/tstruct/auth.json` (`0600`,
outside git).

## Consequences

### Positive
- Architecture-aware layout with clean routing, consistent across all diagrams.
- Switching engines is a one-line manifest change (ELK ↔ TALA ↔ dagre).

### Negative / trade-offs
- TALA is proprietary and **subscription-gated**: if the token lapses, diagrams
  revert to a watermarked evaluation mode (after a 30-day grace). After 12 months of
  subscription the local copy is perpetual.
- Adds an external dependency (the token + occasional renewal ping).

### Neutral / follow-ups
- Fallback path: set `diagram_style.d2.layout: elk` in the manifest to return to the
  bundled, free, orthogonal engine at any time.

## Alternatives considered

- **ELK (bundled, free)** — viable fallback; orthogonal routing but less tuned for
  architecture diagrams. Retained as the documented fallback.
- **dagre (default)** — rejected: curved spline edges, less crisp for this content.
