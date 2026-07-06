# health/ — health & wellbeing

Personal health helpers. Keep these **read-only / logging-only** first; never let
a tool dispense medical advice or change anything that matters without a clear
confirmation step.

**Planned ideas:**
- `log_water` / `log_weight` / `log_workout` — append to a local journal in `data/`.
- `get_activity` — steps / sleep summary (only if you wire a local export; avoid
  cloud fitness APIs that phone home).
- `med_reminder` — schedule a medication/hydration reminder.
- `nutrition_lookup` — calories/macros for a food (e.g. Open Food Facts, no key).

Network tools need a least-privilege egress policy in `agent/policies/`. Add one:
`agent/new-tool.sh log_water --category health`
