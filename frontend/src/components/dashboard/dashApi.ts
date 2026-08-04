// Typed client + types for Ava's dashboard endpoints (Vitals + Operations).
// Same-origin (session cookie sent automatically); 401 -> /login.

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
  if (r.status === 401) {
    window.location.href = '/login';
    throw new Error('unauthorized');
  }
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return (await r.json()) as T;
}

// ---- types ----------------------------------------------------------------
export interface Stat { n: number; avg: number; p50: number; min: number; max: number }
export interface LlmStat {
  count: number;
  tokens_per_sec: Stat | null;
  ttft_ms: Stat | null;
  completion_tokens: Stat | null;
  failovers: number;
}
export interface PerfSummary {
  ok: boolean;
  apps: string[];
  total: number;
  sources_present: Record<string, boolean>;
  summary: {
    records: number;
    llm?: Record<string, LlmStat>;
    actions?: { count: number; seconds: Stat | null; errors: number };
  };
}
export interface SeriesPoint { t: number; [series: string]: number }
export interface PerfSeries {
  ok: boolean;
  metric: string;
  bucket: number;
  series: string[];
  points: SeriesPoint[];
}
export interface CostBreak { spend_usd: number; energy_kwh: number; n: number }
export interface PerfCost {
  ok: boolean;
  since: string;
  spend_usd: number;
  /** NULL when no defensible wattage exists for this platform. Zero would be a
   *  claim about free electricity, and a flat 180 W was a claim about someone
   *  else's GPU — so null is the honest third option. Render it as "—". */
  energy_kwh: number | null;
  energy_usd: number | null;
  /** Provenance of energy_kwh for THIS window: every watt-hour sampled,
   *  some sampled some nominal, none sampled, or no wattage at all. Not "is the
   *  GPU reporting right now" — see power_sampled_now. */
  energy_state: 'measured' | 'partial' | 'estimated' | 'unknown';
  energy_estimated_kwh: number | null;
  /** Where the wattage came from, so the UI can say WHY a figure is an estimate.
   *  "no sensor on this platform" and "you have not declared your card's draw"
   *  need different advice. */
  power_source: 'sampled' | 'declared' | 'platform-nominal' | null;
  power_provenance: string | null;
  /** True only when energy_state === 'measured'. */
  power_measured: boolean;
  /** The live ring buffer has readings. Says nothing about the window. */
  power_sampled_now: boolean;
  avg_gpu_watts: number | null;
  by: Record<string, CostBreak>;
}
export interface HwSample {
  ts: number;
  gpu_util: number | null;
  gpu_temp: number | null;
  gpu_power: number | null;
  mem_used_pct: number | null;
  mem_used_gb: number | null;
  cpu: number | null;
}
export interface TurnRow {
  id: string; status?: string; created?: number; step_count: number;
  last_step?: { kind?: string; name?: string; text?: string } | null;
  tools_used: string[];
  model?: { label?: string; id?: string } | null;
  ctx_tokens?: number | null; error?: string | null;
  reply_preview?: string | null;
}
export interface OpsSummary {
  ok: boolean;
  turns: { running: number; total: number; by_status: Record<string, number> };
  generations_24h: number;
  learning: {
    code: { last_cycle: string | null; cycles: number; pending: number };
    chat: { last_cycle: string | null; cycles: number; pending: number };
  };
  ts: number;
}
export interface Service { name: string; unit?: string; systemd?: string | null; probe_ok?: boolean | null; status: string }
export interface Timer { unit?: string; activates?: string; description?: string; next_time?: string | null; next_rel?: string | null; last_time?: string | null; last_rel?: string | null }
export interface ToolRow { tool: string; count: number }
export interface Alert {
  id: string; severity: string; metric: string; value: number | null;
  threshold: number; op: string; message: string; since: number | null;
}
export interface ConnectorRow {
  id: string; label: string; kind: string; has_service: boolean;
  has_perf: boolean; perf_app: string; perf_present: boolean;
  status: string | null; egress_routes: number; actions: string[];
}

// ---- client ---------------------------------------------------------------
const qs = (o: Record<string, string | number | boolean | undefined>) => {
  const p = Object.entries(o)
    .filter(([, v]) => v !== undefined && v !== '')
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
    .join('&');
  return p ? `?${p}` : '';
};

export const dash = {
  perfSummary: (app?: string, category?: string, since?: string) =>
    get<PerfSummary>('/api/perf/summary' + qs({ app, category, since })),
  perfSeries: (metric: string, bucket = '1h', since = '24h', app?: string, category?: string) =>
    get<PerfSeries>('/api/perf/series' + qs({ metric, bucket, since, app, category })),
  perfRecent: (limit = 50, app?: string, category?: string) =>
    get<{ ok: boolean; recent: Record<string, unknown>[] }>('/api/perf/recent' + qs({ limit, app, category })),
  perfCost: (since = '7d', group = 'model') =>
    get<PerfCost>('/api/perf/cost' + qs({ since, group })),
  // daily_energy_kwh is NULLABLE — /api/hub/cost passes perf_cost's figure
  // straight through, and that is null on any platform with no sampled, declared
  // or nominal wattage. hub/hubApi.ts already types it that way; this copy did
  // not, which is how VitalsView's budget bar came to call .toFixed() on null.
  budget: () => get<{
    currency: string; daily_spend_usd: number; daily_energy_kwh: number | null; power_measured: boolean;
    budgets: { daily_usd: number | null; monthly_usd: number | null; daily_kwh: number | null };
  }>('/api/hub/cost'),
  hwHistory: (since = '1d', bucket = '5m') =>
    get<{ ok: boolean; samples: HwSample[] }>('/api/hardware/history' + qs({ since, bucket })),
  turns: (active = false, limit = 50) =>
    get<{ ok: boolean; turns: TurnRow[] }>('/api/turns' + qs({ active, limit })),
  opsSummary: () => get<OpsSummary>('/api/ops/summary'),
  services: () => get<{ ok: boolean; services: Service[]; down: number }>('/api/ops/services'),
  schedule: () => get<{ ok: boolean; timers: Timer[] }>('/api/ops/schedule'),
  tools: (limit = 15) => get<{ ok: boolean; tools: ToolRow[]; total_calls: number }>('/api/ops/tools' + qs({ limit })),
  alerts: () => get<{ ok: boolean; active: Alert[]; metrics: Record<string, number> }>('/api/ops/alerts'),
  connectors: () => get<{ ok: boolean; connectors: ConnectorRow[]; action_count: number }>('/api/ops/connectors'),
};
