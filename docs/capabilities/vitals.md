# Vitals - what Ava measures about herself

Every assistant claims to be fast. Ava writes down what actually happened, one
record per generation, on your disk, in a plain text format you can open in
any editor. **Vitals** is the page that reads it back.

That is the whole design: the dashboard is not a decoration painted over a
guess, it is a rendering of an append-only log. Every number below traces to a
line you can open yourself.

![The Vitals dashboard: a six-tile KPI strip, a today's-budget meter, an inference-throughput chart with a Day/Week/Month range dropdown beside a model-routing donut, generation-performance and energy-by-app bar lists, and the connected-apps panel](../assets/vitals-dashboard.png)

## What is *not* collected

This matters more than any number on the page:

- **No per-process telemetry.** The sampler reads device-level counters -
  GPU, memory, CPU - not what any process is doing.
- **No per-user telemetry.** There are no users to attribute to; Ava is a
  single-owner system.
- **No content telemetry.** No prompt text, no reply text, no message bodies
  are ever written to the perf log. An image record stores the prompt's
  *character count*, not the prompt.
- **Nothing leaves the machine.** There is no phone-home, no analytics
  endpoint, no vendor dashboard. The files are on your disk and only you read
  them.

The vitals stream is device metrics plus generation timings. That is the
whole of it.

## The KPI strip

![The six KPI tiles at native size: Spend (7d) $0.00 from 7 sources; Energy (7d, est.) 6.970 kWh, sub-labelled "estimate, 214 W typical for this platform, not measured here"; Throughput 56.0 tok/s over 16,594 completions; TTFT 0.44 s; Renders 3,055; Route errors 0](../assets/vitals-kpi-strip.png)

Six tiles. Each has a hover hint that explains in plain language what the
number means, and the glossary lives in one file
(`frontend/src/components/dashboard/metrics.ts`) reused across Vitals and
Operations, so the same metric never gets two explanations.

| Tile | What it is | Where it comes from |
|---|---|---|
| **Spend (7d)** | Cloud API dollars in the window. Running locally is free - this only moves when a paid cloud model answers. | `GET /api/perf/cost?since=7d&group=app` |
| **Energy (7d)** | GPU kilowatt-hours. Labelled **"Energy (7d, est.)"** unless every watt-hour in the window was sampled - which for a 7-day window it essentially never is (see below). The hint distinguishes "part measured" from "no GPU power sensor". | same call; `energy_state` (`measured`/`partial`/`estimated`) decides the label |
| **Throughput** | Average tokens/sec across models, weighted by completion count. Sub-line shows how many completions it averaged. | `GET /api/perf/summary` |
| **TTFT** | Average time to first token: how long before Ava starts replying. | same summary |
| **Renders** | Image + video generations completed, with upscales on the sub-line. | same summary |
| **Route errors** | How many times the inference router fell back to a backup backend. Zero is the healthy state. | `failovers` count in the summary |

The **Today's budget** meter appears above the charts only once you have set a
daily cap in **Setup → Hardware**. With no cap, the panel is not rendered at
all rather than shown empty.

## One record per generation

Everything on this page is derived from `logs/performance.jsonl`, where
**every generation appends exactly one line**: what ran, on which machine,
which backend answered, how many tokens, how fast, how long. Nothing is
overwritten.

Three claims follow from that, and they are the reason the page is worth
trusting:

- **Agent turns are counted.** The agent bypasses the inference router, so the
  runtime writes its own record per turn. Without that, Vitals would sit empty
  on a default install while you chatted all day.
- **Connected apps write the same schema**, in their own repos, and Vitals
  reads all of them at once. Adding or removing an app in
  **Setup → Connectors** changes Vitals immediately, with no restart - and
  removing an app does not erase its history.
- **Long ranges are cheap.** A recent window is served from the raw file;
  anything older is served from pre-aggregated buckets, so a 1-year chart
  never rescans a year of raw lines.

Set `AVA_PERF_LOG=0` to turn generation logging off entirely.

??? note "The record format, the writers, and how rollups stay correct"

    The writer is `perf_log.log_perf()`, which always stamps four fields and
    drops any `None` so lines stay compact:

    | Always stamped | Meaning |
    |---|---|
    | `ts` | epoch seconds |
    | `iso` | local wall clock |
    | `host` | the machine that generated it |
    | `category` | `llm`, `image` or `upscale` |

    An LLM completion served through the inference router adds the facts the
    router knows: the serving engine, the endpoint, which backend answered,
    whether it was a failover, the HTTP status, prompt/completion/total
    tokens, tokens per second, time to first token, generation seconds, and
    the sampling parameters used. A real line, straight off disk (hostname
    stubbed):

    ```json
    {"ts":1785258062.146,"iso":"2026-07-28T10:01:02","host":"ava-host",
     "category":"llm","serving":"vllm","endpoint":"chat/completions",
     "served_by":"b1","served_model":"test-model","status":200,
     "prompt_tokens":3,"completion_tokens":2,"ttft_ms":0.4,"gen_seconds":0.0}
    ```

    Image renders record the checkpoint, steps, cfg, sampler/scheduler,
    resolution, seed, refine_hi settings, render seconds and steps/sec. Upscales
    record the model and the time it took.

    Writes are best-effort and **never raise into the generation path** - a
    full disk costs you a log line, not a reply. The two writer processes (the
    router and the bridge) serialise with an `flock` so concurrent lines never
    interleave.

    **Agent turns count too.** When the agent runtime answers a turn it goes
    straight to the sandbox model and **bypasses the inference router**, which
    was historically the only writer of `llm` records. So the NemoClaw runtime
    writes its own record per turn (`ava_bridge/runtime/nemoclaw.py`):
    duration, the sandbox model, and token usage *only when the runtime
    actually reports it*. Nothing is fabricated to fill a chart, and the
    telemetry never breaks a turn.

    **Connected apps write the same schema.** Each app Ava drives keeps its
    own `performance.jsonl` in its own repo, in the identical schema, and
    declares the path in its connector manifest. Removing an app does **not**
    erase its history: a small on-disk ledger remembers every perf source ever
    seen, so the charts keep the past and resume seamlessly if you re-add the
    app under the same id.

    **Rollups that compose.** A recent window is served straight from raw
    JSONL (the *hot* window, `perf.hot_window`, default `48h`). Anything older
    is served from pre-aggregated buckets. Two design decisions make that
    safe:

    *An incremental watermark.* Each rollup pass finalizes only records in
    `[watermark, now − hot_window)`, accumulates them into existing buckets,
    then advances the watermark. Because a bucket has already absorbed its raw
    lines, deleting those raw lines cannot lose history - and re-running the
    rollup cannot double-count.

    *Mergeable bucket form.* Buckets store **sums, counts and fixed-bound
    histograms - never bare averages.** That is the property that makes ranges
    composable: two buckets (or two windows) combine by adding, a weighted
    average over any range is `Σsum / Σcount`, and a p50/p90/p95 over any
    range is derived by summing histograms and interpolating within the
    containing bin. Fixed histogram bounds are used precisely for that
    compose-across-windows property; nothing here speaks Prometheus.

    The rollup runs **in-process on a daemon thread** - the same pattern as
    the hardware sampler - so it behaves identically on a bare host, in
    Docker, or on a Mac. There is no systemd timer to install.

    ```yaml
    perf:
      hot_window: 48h        # recent range served from raw JSONL
      rollup_interval: 1h    # how often the in-process rollup runs
      max_rotated_files: 5   # raw segments retained before the rollup absorbs them
      max_tok_s: 2000        # clamp implausible tokens/sec to null
    ```

    `max_tok_s` exists because a one-token or cached reply can round
    `gen_seconds` to near zero and produce a "556k tok/s" outlier that wrecks
    every average it touches. The clamp is applied in both the rollup store
    and the reader, so the dashboard and the agent's own perf tool report the
    same numbers.

## The hardware sampler

A background thread records a compact sample **every 5 seconds**
(`AVA_HW_SAMPLE_INTERVAL`) into an in-process ring buffer holding roughly two
hours. It captures exactly six fields, and nothing else: GPU utilisation, GPU
temperature, GPU power, memory used, memory percent, and CPU.

The ring dies on restart, so a second thread rolls it into two bounded on-disk
tiers under `logs/hw_history/`:

| Tier | Cadence | Kept for |
|---|---|---|
| `hw_1m.jsonl` | 1-minute averages | `data.retention_days`, hard-capped at **90 days** regardless of the setting |
| `hw_1h.jsonl` | 1-hour averages | `data.retention_days` (default 183 days; `0` = forever) |

??? note "The sample line, and how a chart range picks its tier"

    ```json
    {"gpu_util":0.0,"gpu_temp":38.0,"gpu_power":5.55,
     "mem_used_pct":56.0,"mem_used_gb":68.19,"cpu":15.55,"ts":1785258058.6}
    ```

    The ring only covers a couple of hours, so it cannot back the Week /
    Month / Year filters. `GET /api/hardware/history` picks the tier that
    covers the window (1-minute for ≤ 90 days, 1-hour beyond), stitches the
    hot ring-buffer tail on top, and averages into whatever bucket the chart
    asked for. Compact JSONL, no database. Everything is wrapped so a disk
    error can never take sampling down.

### One snapshot, two renderings

<!-- TODO: a cropped screenshot of the expanded hardware bubble belongs here. -->

The floating **hardware bubble** is on every view, draggable, and remembers
where you put it. Tap it for GPU, memory, disk, CPU, temperature and the
models. **Ava's brain is listed first, badged, and shown even when nothing is
running**, because which model answers is named by your configuration, not
discovered from the GPU. It reads the same live snapshot the Vitals gauges
use, so the two can never disagree.

??? note "Every state a model row can report, and the poll cadence"

    The bubble is rendered outside the view switch, so it is present on Chats,
    Vitals, Operations, Data, Setup and every connected-app tab. Expanded, it
    shows GPU util, running jobs, unified memory, disk, CPU, GPU temperature,
    and the models.

    Every model row states what was actually *observed* about it: in memory,
    ready but not loaded (an idle Ollama evicts after a few minutes and
    reloads on the next message), not downloaded yet, engine offline, runs
    elsewhere, or - honestly - not observable. **"Not observable" never means
    "not loaded".**

    It polls every 2 s while open and every 5 s while collapsed, reading
    `GET /api/hardware` - **the exact same live snapshot** the four gauges and
    the watts readout on the Vitals hardware panel use. One source, two
    renderings. (The charted range underneath still comes from the bucketed
    history above.)

## Cost and energy

Money and electricity are estimated from the same records, using two inputs
you control:

- **An electricity rate** (`cost.electricity_rate_per_kwh`, editable in
  **Setup → Hardware**) turns GPU energy into dollars. It is an estimate, Ava
  labels it as one, and over any window longer than two days it is mostly a
  nominal figure rather than a reading.
- **Per-model prices** (`config/cost.yaml`, USD per million tokens, matched
  as a substring against the served model name, longest match wins) turn
  prompt and completion tokens into spend. Local models have no entry, so
  they cost \$0 in API fees - only electricity.

??? note "Exactly how energy is computed, and what `energy_state` reports"

    Energy is average GPU power × generation seconds - one window-wide average
    applied to every record, not a per-generation power reading. Records
    inside the hot window (`perf.hot_window`, 48 h) use an average of recent
    real power samples; **everything older comes from rollups whose
    watt-hours are `nominal_gpu_watts × seconds`, so a long window is mostly
    nominal.**

    The API returns `energy_state` alongside `energy_estimated_kwh`:

    | `energy_state` | Meaning |
    |---|---|
    | `measured` | the whole window was sampled |
    | `partial` | only the hot tail was sampled |
    | `estimated` | nothing was sampled |

    The UI is required to label an estimate as an estimate rather than present
    it as a measured dollar figure.

### Budgets alert; they never block

Three optional caps live in `cost.budgets` and are editable in
**Setup → Hardware**:

| Cap | Key |
|---|---|
| Daily cloud spend | `cost.budgets.daily_usd` |
| Monthly cloud spend | `cost.budgets.monthly_usd` |
| Daily GPU energy | `cost.budgets.daily_kwh` |

Leave one blank and it is off; its meter reads "no cap set" and its alert rule
stays dormant. Set one and the percentage-of-budget becomes a metric the alert
engine evaluates: daily cloud spend warns at 80 % and goes critical at 100 %,
monthly cloud spend goes critical at 100 %, daily GPU energy warns at 100 %.

**Nothing is ever stopped.** Hitting a cap raises an alert on
[Operations](operations.md) and nothing else. A budget that silently killed a
request mid-answer would be a worse failure than the overspend it prevented.

A related rule needs no cap at all: *idle burn* counts completion tokens
generated in the last 10 minutes while no turn is running and more than two
minutes after your last interaction, and warns above 5,000 - the "what did it
spend while I slept" number.

The Budgets panel converts today's kWh into money at your rate, so the two
costs read in the same terms instead of forcing you to do the arithmetic.

## The panels

Every time series shares one range selector, defined once so a range added
there appears everywhere with its axis already correct.

| Panel | What it shows |
|---|---|
| **Inference throughput** | Tokens/sec over time, one line per model. Its empty state is honest about *why* it is empty: "turns are recorded, but no token-rate samples in this range" is a different problem from "no inference recorded yet", and it says which. |
| **Model routing** | A donut of each model's share of completions. On a default agent install every completion carries the same label - the sandbox model - so it reads as a single slice until you add a second backend. |
| **Generation performance** | Average seconds per render pass, one bar each for image renders, video renders and upscales, hiding any that has no data. |
| **Energy by app (7d)** | Estimated kWh grouped by app, from the same cost call as the KPI strip. |
| **Connected apps** | A grid built live from the connector registry, external apps only. Each card shows 7-day call count, energy and action count - or, before an app has done anything, the honest **"connected · no metrics yet - history starts with its first call"** instead of a zero pretending to be a measurement. |
| **Hardware** | Four gauges (GPU util, GPU temp, memory, CPU) and a live watts readout from the instantaneous snapshot, over a multi-series chart of the bucketed history. |

??? note "The range selector: windows, buckets and refresh rates"

    | Range | Window | Bucket | Refresh |
    |---|---|---|---|
    | Day | 1 d | 5 m | 5 s |
    | Week | 7 d | 1 h | 30 s |
    | Month | 30 d | 6 h | 60 s |
    | 3 months | 90 d | 1 d | 60 s |
    | 1 year | 365 d | 1 d | 60 s |
    | 5 years | 1825 d | 7 d | 60 s |

## Where it all lives

```
$AVA_HOME/logs/
  performance.jsonl        raw generation records (rotates at 32 MiB × 5)
  rollups/
    perf_hourly.jsonl      hourly buckets: sums + counts + histograms
    perf_daily.jsonl       daily buckets, same shape
    .watermark             how far the rollup has finalized
  hw_history/
    hw_1m.jsonl            1-minute hardware averages (≤ 90 d)
    hw_1h.jsonl            1-hour hardware averages
```

**Flat files.** No Prometheus, no InfluxDB, no external time-series database,
no scrape target, nothing to run alongside Ava and nothing extra to back up.
The only database anywhere in Ava is `data/memory.db`, the SQLite store behind
[long-term memory](../MEMORY.md) - and Vitals never touches it.

Raw rotation is non-destructive: `performance.jsonl` → `.1` → `.2` and only
the segment past `perf.max_rotated_files` is dropped, *after* the rollup has
absorbed it. A generation of history is never silently discarded.

Every store here is listed, sized and browsable on the [Data](data.md) page,
which also documents exactly what `data.retention_days` does and does not
reach.

## Limitations (honest edition)

What this page cannot tell you, stated plainly. The first group is about
estimates; the second is about coverage.

### Energy and money are estimates

- **Energy is always partly an estimate.** With no GPU power samples, a nominal
  wattage stands in for everything. With samples,
  only the last `perf.hot_window` (48 h) uses them - older buckets are nominal,
  so any window longer than two days is mostly estimated and the tile says
  "est." accordingly. Treat the number as an order of magnitude.
- **Per-app energy is a time-share attribution, not a per-app measurement.**
  Whole-GPU wattage is multiplied by each app's own seconds, so two apps
  generating concurrently are each charged the full GPU and the split can
  exceed total draw. It answers "which app is responsible for the most GPU
  time" honestly; it is not a per-app power meter.
- **Where the wattage comes from is reported, and there is no global default.**
  `power_source` is one of `sampled` (this GPU's own readings), `declared` (you set
  `cost.nominal_gpu_watts`), `platform-nominal` (a typical figure for your detected
  platform, from `deploy/platforms.conf`), or `null`. In the last case
  `energy_kwh` is **null** and the tile reads *not measured* - zero would be a
  claim about free electricity. `nominal_gpu_watts` used to default to a flat
  180 W, which is roughly a mid-range discrete NVIDIA card and wrong by up to an
  order of magnitude for a Mac mini (~10-30 W) or a Strix Halo APU (~50-120 W).
- **Currency is gated harder than kilowatt-hours.** `energy_usd` is withheld
  unless the wattage was *sampled* or *declared*: a kWh figure can carry an
  "(est.)" label and still inform, whereas a dollar amount reads as settled. A
  platform nominal therefore shows energy but no money.
- **Cost is only as good as the price table.** A cloud model with no entry in
  `config/cost.yaml` contributes \$0 to spend. Add its key if you route to it.

### Coverage and history have edges

- **Throughput needs a cooperative endpoint.** Tokens/sec and TTFT are only
  recorded when the serving endpoint reports usage. An agent turn whose
  runtime returns no usage block still logs its duration and model - it just
  contributes no token rate.
- **A boundary bucket may under-report.** Where the hot tail meets the cold
  rollups, one bucket can reflect only its hot portion (fresh raw wins on a
  key collision). Negligible for a trend line, worth knowing before you read
  a single bucket as gospel.
- **Long ranges are bounded by retention.** A 5-year chart on a two-week-old
  install shows two weeks; the hardware minute tier never reaches past 90
  days no matter what retention says.
- **Vitals reports memory pressure; it does not resolve it.** Which model
  holds memory, who yields to whom, and how leases are decided is a separate
  system - see [Running two models](../ALLOCATION.md).
