# Chart theme integration validation

Validated against the running Ava and Analytics deployment on September 13, 2026.

## Behavior

Ava's existing light/dark toggle drives the native Superset theme in both the
chat preview and conversation visualization panel. Theme changes redraw charts
without navigating the iframe or rerunning the chart query. Reopening a saved
session uses Ava's current preference. Sources and the owning chat stay attached.

The Analytics proxy validates the parent frame and configured origin, preserves
light/dark context through internal redirects, and injects its receiver with the
upstream CSP nonce before bootstrap. Superset uses the canonical Analytics
colors for light/dark surfaces, labels, axes, legends and tooltips. Its layout
background explicitly uses the measured chart surface, including legacy maps.
Categorical slot order and scheme identity are preserved with colors that meet
3:1 contrast on both chart surfaces. Map/heatmap scale meanings are unchanged.

A small build-time adapter exposes Superset 6.1.0's native ThemeController readiness.
The image build fails if the expected constructor changes or is ambiguous; an
upstream upgrade requires reviewing this adapter. It calls the public theme API
and does not replace the chart renderer or modify saved chart definitions.

Older recorded bar/table results use Ava's DOM/SVG presentation with live CSS
colors. Stored result values, uncertainty and immutable download files are unchanged.

## Validation

- Ava frontend: 430 tests passed; production build and changed-file lint passed.
- Ava whole-app backend QA in Linux: 113 passed, 3 skipped.
- Analytics web: 581 tests passed; TypeScript check and changed-file ESLint passed.
- Superset image: 19 tests passed, including native theme and adapter contracts.
- Canonical generated theme parity and changed-file Ruff checks passed.
- Superset, Analytics web and Ava production images built and rolled healthy.

### Live chart matrix

Each example was tested in light, dark and light again through Ava's actual theme
button, with a native renderer in both the preview and expanded panel. The tests
checked the native controller theme, actual rendered text and surfaces, document
identity, chart requests, source links and owning session. Screenshots were reviewed
for canvas charts and both maps; hover checks covered native chart and map tooltips.

| Visualization | Native renderer | Light/dark/live toggle |
| --- | --- | --- |
| Bar | ECharts time series bar | Passed |
| Pie | ECharts pie | Passed |
| Donut | ECharts pie/donut | Passed |
| Line | ECharts time series | Passed |
| Area | ECharts time series | Passed |
| Heatmap | ECharts heatmap | Passed |
| Scatter/bubble | ECharts bubble | Passed |
| US map | Country map SVG | Passed |
| World map | World map SVG | Passed |

The US map, world map and donut also passed saved-session reload in dark mode,
expanded/split view, 390px mobile layout, returning to the composer, and reopening
in light mode. A previously recorded Census bar chart retained identical values
while its mark and text colors changed with Ava. The existing **Superset charts**
sessions remain available for manual review.

Owner-local evidence and replay harnesses are in ignored `logs/theme-work/`,
`logs/theme-validation.mjs` and `logs/theme-interactions.mjs`. Authentication was
held in memory; temporary Analytics test sessions were removed after each run.
No new model queries or saved chart definitions were needed for these checks.
