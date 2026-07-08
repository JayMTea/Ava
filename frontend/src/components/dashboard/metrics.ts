// Plain-language explanations for every metric shown in the dashboards, so a
// hover ⓘ can tell the user what a number actually means. One glossary, keyed by
// a short id, reused across Vitals / Operations / Learning. Keep each entry to a
// sentence or two — this is a tooltip, not documentation.

export const METRICS: Record<string, string> = {
  // ---- Vitals · KPI strip ----
  spend:
    'Cloud API dollars spent in this window. Running Ava on your own hardware is free — this only moves when she calls a paid cloud model.',
  energy:
    'Estimated electricity the GPU used, in kilowatt-hours. Measured directly if your GPU reports power draw, otherwise estimated from a nominal wattage.',
  throughput:
    'Average generation speed in tokens per second, across all models. Higher means Ava writes replies faster.',
  ttft:
    'Time to first token — how long after you send a message before Ava starts replying, in milliseconds. Lower feels snappier.',
  renders:
    'Number of image and video generations completed in this window.',
  routeErrors:
    'How many times the model router had to fall back to a backup model because the primary failed. Zero is ideal.',

  // ---- Vitals · Hardware gauges ----
  gpuUtil:
    'How busy the GPU’s compute cores are right now, 0–100%. High during generation, near zero when idle.',
  gpuTemp:
    'GPU core temperature in °C. Sustained high temperatures can make the card throttle and slow down.',
  memory:
    'Share of GPU/system memory in use, 0–100%. Close to 100% risks out-of-memory errors during large jobs.',
  cpu:
    'Overall CPU utilisation across all cores, 0–100%.',
  gpuPower:
    'Instantaneous GPU power draw in watts — the live number behind the energy estimate.',

  // ---- Vitals · charts ----
  throughputSeries:
    'Tokens per second over time, one line per model. Use the range buttons to zoom from a single day out to five years.',
  hardwareSeries:
    'GPU utilisation, temperature, memory and CPU over time. The range buttons control how far back the chart reaches — limited by your data-retention setting.',
  modelShare:
    'Share of completions handled by each model — shows which brain is doing the work.',

  // ---- Learning / Control Center ----
  approvalGates:
    'Changes Ava has proposed that are waiting for your approval before anything is applied.',
  codeChanges:
    'Proposals that modify code, as opposed to plain ideas or notes.',
  completed:
    'Proposals you approved and Ava has applied.',
  apps:
    'Apps Ava is connected to and can propose changes for. Ava herself is always included; other apps appear only while connected.',

  // ---- Operations · KPI strip ----
  activeTurns:
    'Chat turns Ava is processing right now — a turn is one request/response exchange.',
  activeRenders:
    'Image or video generation jobs currently running.',
  generations24h:
    'Total image + video generations completed in the last 24 hours.',
  servicesUp:
    'Backend services (model engine, connectors, schedulers) that are up, out of the total Ava monitors.',
  pendingApprovals:
    'Learning proposals waiting for your approval on the Control Center below.',
  alerts:
    'Active alerts — budget, hardware, or service issues Ava has flagged. Red means at least one is critical.',
};
