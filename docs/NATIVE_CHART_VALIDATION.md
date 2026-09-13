# Native Superset chart validation

Validated against the running Ava and Analytics deployment on September 13, 2026.

## Change

Analytics previously attached an immutable bar-chart artifact to analysis results.
That renderer could not display native Superset chart types or geographic maps.
The connector now discovers saved Superset charts with `search_charts` and returns
their native chart views with `show_chart`. Ava displays these live views inside
the owning conversation, with a compact preview, an expanded pane, and citations.
Existing recorded artifacts remain supported.

The integration passes through saved visualization types, including installed
Superset plugins. It does not create arbitrary new chart definitions. Search
returns the measures and saved scope so the agent can choose a matching chart;
literal column filters can narrow or change that scope without replacing the
renderer or unrelated saved constraints.

The tool requires an explicit filter list. Search keywords and previous analysis
calls do not implicitly scope a chart. An omitted filter decision is rejected
before retrieval; an empty list means the saved chart already matches the
question. This addresses missing state filters found during ordinary chat tests.
The Census chart adapter also normalizes numeric state codes (`6` becomes `06`)
and rejects calendar years supplied as survey durations. Both protections apply
when retrieving a chart and when reopening a previously saved native artifact.

## Live browser checks

Nine examples are saved in Ava under **Superset charts**. The validation runner
called the actual consent-gated connector tools and saved their returned artifacts
through Ava's conversation store. These examples are tool integration checks;
separate normal chat queries exercise the agent's chart selection.

| Requested view | Native renderer | Verified result |
| --- | --- | --- |
| US map | `country_map` | ACS 2024 1-year; 51 state/DC observations |
| World map | `world_map` | Latest World Bank population comparison; geographic shapes rendered |
| Pie | `pie` | California, Oregon, Washington only |
| Donut | `pie`, donut enabled | California and Oregon only |
| Line | `echarts_timeseries_line` | California/Oregon across ACS 2022–2024 |
| Area | `echarts_timeseries_line`, area enabled | California/Oregon across ACS 2022–2024 |
| Heatmap | `heatmap_v2` | Three states across ACS 2022–2024 |
| Bar | `echarts_timeseries_bar` | California and Oregon only |
| Scatter/bubble | `bubble_v2` | GDP per capita and life expectancy at the saved common year |

All nine native views returned successful chart queries, rendered a canvas or map,
retained source citations, and stayed inside the conversation's chart bounds.
The desktop chat composer remained accessible. Reload checks passed for both maps
and the scoped donut; returned donut rows retained the two-state filter after
reload. A 390×844 mobile check passed for the US map, including returning to chat.

Eight completed normal chat prompts also passed browser checks for the requested
native type, actual returned geography/year scope, citations, and conversation
layout: US map, world map, pie, donut, line, area, heatmap, and scatter. The pie
check specifically verified California remained present when the agent supplied
state code `6`. These sessions are saved under **Superset queries**.

Exploratory attempts exposed omitted filters, a calendar-year/duration mix-up,
and numeric state-code formatting; the fixes above address these cases. Other
attempts were interrupted by deployment or ended with the agent returning no
answer. They remain labeled **Validation history**, rather than being counted
as passes. The area query's generated claim of steady yearly growth was also
incorrect: both states dipped in 2023 before increasing in 2024. A correction
was appended to that session. The chart checks establish rendering and scope
correctness, not general reliability of the model's prose interpretation.

Browser validation caught a Superset 6 redirect that ignored inline filter state.
The integration now uses Superset's temporary form-data cache after authentication,
creating a fresh key when the saved artifact is reopened. Assertions inspect
actual returned rows, rather than only checking the chart title or saved metadata.
The isolated app origin is explicitly allowed through the existing proxy's CSRF
origin handling. Authentication, consent, and CSRF protection remain enabled.

## Automated checks

- Analytics Python suite: **1,248 passed, 24 skipped**. Subsequent native-chart and
  MCP checks: **46 passed**, including rejection of an omitted filter decision.
- Analytics web suite: **566 passed**; TypeScript, lint, and production build passed.
- Superset image unit tests: **15 passed**, including literal scope preservation,
  Census state-code normalization, and calendar-year/survey-duration validation.
- Ava frontend suite: **429 passed**; production build and lint passed (existing warnings).
- Ava native artifact/tool checks: **33 passed, 1 skipped, 4 subtests passed**.
- Python lint/type checks and generated Analytics catalog checks passed.

The full Ava Python run had **2,730 passed, 31 skipped, and three failures** in
untouched verifier-copy, diagram-sync, and first-run installer tests. The same
three checks failed in a separate checkout used to investigate line endings.
This validation does **not** establish a green full Ava CI run.

Live artifacts reopen the saved native chart with current data and require the
user's Analytics access. They are not immutable data snapshots. Census values
are weighted PUMS estimates; country map topology can omit countries without a
matching polygon, and missing observations are not represented as zero.
