# Ava — working conventions

Ava is a private, self-hosted AI assistant (FastAPI bridge `phone_bridge.py` +
`ava_bridge/`, React SPA in `frontend/`, agent runtime + tools in `agent/`).
Every feature must stay fork-and-self-host packageable — see
docs/PACKAGING_PLAN.md.

## Capabilities: the feature registry (enforced by tests)

Any user-facing optional capability lives in `ava_bridge/features.py`:

- **One registry entry** gives it the Setup → System → Optional features
  checkbox, the setup-save whitelist, and guided-fix error codes.
- **Gate its execution path** with `features.preflight(key, probe=...)` —
  never a hand-rolled `settings.get_bool("features.…")`.
  `tests/test_feature_convention.py` fails any bypass, repo-wide.
- Preflight yields regular codes: `<key>_off` (switch off) / `<key>_down`
  (switch on, service unreachable). The chat UI derives fix-it links from the
  code *pattern* (`frontend/src/lib/fixes.ts`) — new capabilities need zero
  frontend changes.
- Coded errors on `/internal/*` routes ship as **HTTP 200** bodies
  `{"error", "error_code"}`: the sandbox tool helper uses `curl --fail` and
  swallows non-2xx bodies, and the message must reach Ava so she can tell the
  user how to fix it.
- Full write-up: docs/CONNECTOR_SDK.md §6.

## Other conventions

- **Backend returns facts; owner-facing copy lives in the frontend** (see
  frontend/src/components/data/DataView.tsx header comment). Exception:
  registry labels/messages, which must be self-contained for agent tools.
- **Connected-app identity accents:** any UI element that represents a
  connected app — nav entries, chat tool chips, artifact/preview cards, app
  view headers, future indicators — must carry the app's accent color via
  `appAccent()` / `<AppDot>` from `frontend/src/lib/appColor.tsx` (manifest
  `ui.color` override, else a stable auto color from `--app-accent-*` tokens).
  Never style an app-owned indicator as if it were Ava's own.
- **App icons follow the same rule:** render them via `appIcon()` from the same
  module, never a raw `entry.icon` — undeclared icons come back `null` on
  purpose so `appIcon()` can hash the app id into a stable glyph. Never
  reintroduce a fixed backend fallback (an `or "grid"` in `connectors.apps()`
  is what once made every added app look identical). Owners override both
  icon and accent in Setup → Connectors → Appearance, which writes `ui.icon` /
  `ui.color` back to the manifest — the single source of truth.
- Frontend changes only take effect via the built bundle: run
  `cd frontend && npm run build` (includes tsc). Verify UI changes with the
  whole-app suite: `qa/run.sh --backend` (pytest tiers, no browser) or
  `qa/run.sh` for the Playwright tier against a real bridge subprocess. The
  fixture-contract test additionally needs an owner-local capture harness and
  skips cleanly without it.
- **Setup (Hub) UI is one system.** Every Setup tab lives in its own
  `frontend/src/components/hub/panels/*.tsx` (HubView.tsx is just the tab
  router) and builds from the shared pieces, not per-panel copies: the
  data/action hooks in `hub/hooks.ts` (`useResource`/`useAction` — never a
  hand-rolled load or a hardcoded `hub-msg err`), the view primitives in
  `hub/ui/` (`Tile`, `Legend`, `Badge`, `StatRow`, `HubMessage`), and one
  `.tone-*` → `--tone` colour system in `hub.css` (no per-panel icon-tile or
  tone-colour classes). `tests/test_hub_uniformity.py` fails a regression.
- Python: `ruff check`, tests with `python -m pytest tests/ -q`.
- Convention guards follow the `tests/test_diagram_sync.py` style: static
  scans over `git ls-files` that run anywhere, failing with instructions.
