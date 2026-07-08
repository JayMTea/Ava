// Typed client for the setup/onboarding Hub. Same-origin (session cookie sent
// automatically); a 401 bounces to the server-rendered /login. Reuses the shared
// `req` helper so auth + error handling match the rest of the app.
import { req } from '../../lib/api';

// ---- Models / inference -----------------------------------------------------
export interface HardwareInfo {
  fit_gb: number | null;
  source: string | null;
  tier: string;
  hint: string;
  gpu: string | null;
}
export interface BackendProbe {
  vllm: boolean;
  ollama: boolean;
  router: boolean;
}
export interface SetupConnector {
  id: string;
  label: string;
  kind: string;
}
export interface SavePayload {
  inference?: {
    mode: 'local' | 'cloud';
    engine?: string;
    base_url?: string;
    model?: string;
    api_key?: string;
  };
  features?: Record<string, boolean>;
  connectors?: string[];
}

// ---- Agent runtime ----------------------------------------------------------
export interface AgentStatus {
  name: string;
  available: boolean;
  runtime: string;
  required: boolean;
  cli: string | null;
  sandbox: string | null;
  sandbox_exists: boolean | null;
  health: unknown;
  tools: boolean;
}

// ---- Connectors (Hub view) --------------------------------------------------
export interface HubConnector {
  id: string;
  label: string;
  kind: string;
  status: string; // up | down | unknown | n/a
  actions: number;
  mcp: boolean;
  has_policy: boolean;
  has_tools: boolean;
  renders_policy: boolean;
  enabled: boolean;
}
export interface GenerateResult {
  ok: boolean;
  policy?: string; // rendered YAML preview
  tools?: { name: string; source: string }[];
  wrote?: string[];
  error?: string;
}

// ---- Model store ------------------------------------------------------------
export interface ModelRole {
  role: string;
  id: string;
  engine: string;
  tier?: string;
  present: boolean;
}
export interface ModelStore {
  roles: ModelRole[];
  detected_tier: string;
  available_gb: number | null;
  store: string;
}
export interface PullStatus {
  status: 'idle' | 'running' | 'done' | 'error';
  role: string | null;
  rc: number | null;
  log: string[];
}
export interface BenchResult {
  ok: boolean;
  id: string;
  model: string;
  engine?: string;
  ttft_ms?: number;
  tok_s?: number;
  tokens?: number;
  total_s?: number;
  estimated_tokens?: boolean;
  error?: string;
}
export interface BenchStatus {
  status: 'idle' | 'running' | 'done' | 'error';
  result: { prompt?: string; results: BenchResult[]; winner?: string | null; error?: string } | null;
}

// ---- Voice ------------------------------------------------------------------
export interface VoiceStatus {
  enabled: boolean;
  deps_ok: boolean;
  deps_error: string;
  enrolled: boolean;
  threshold: number;
}
export interface EnrollResult {
  ok: boolean;
  error?: string;
  seconds?: number;
  windows?: number;
  dropped?: number;
  consistency?: { min: number; mean: number; max: number };
  suggested_threshold?: number;
  low_consistency?: boolean;
}

// ---- New connector form -------------------------------------------------------
export interface NewConnectorBody {
  id: string;
  label?: string;
  kind?: string;
  probe?: string;
  base_url?: string;
  token_env?: string;
  confirm?: boolean;
  actions?: { id: string; method: string; path: string; description?: string; confirm?: boolean }[];
  mcp?: { url?: string; command?: string; token_env?: string; sandbox?: string };
  discover?: { base?: string; list?: string; call?: string; token_env?: string };
}
export interface ProbeResult {
  ok: boolean;
  kind?: 'mcp' | 'discover' | 'rest' | 'unknown';
  transport?: string;
  tools?: { name: string; description: string }[];
  detail?: string;
  error?: string;
}

// ---- Cost & budgets ---------------------------------------------------------
export interface CostSettings {
  electricity_rate_per_kwh: number;
  currency: string;
  nominal_gpu_watts: number;
  budgets: { daily_usd: number | null; monthly_usd: number | null; daily_kwh: number | null };
  daily_spend_usd: number;
  daily_energy_kwh: number;
  power_measured: boolean;
}

// ---- Approvals (human-in-the-loop) ------------------------------------------
export interface PendingApproval {
  id: string;
  connector: string;
  action: string;
  args: Record<string, string>;
  ts: number;
}

// ---- Flight recorder (audit ledger) -----------------------------------------
export interface AuditEvent {
  ts: number;
  kind: string;
  status?: string;
  tools?: string[];
  model?: string;
  duration_s?: number;
  error?: string;
  outcome?: string;
  project?: string;
  commit?: string;
  actor?: string;
  approved_by?: string;
  paths?: string[];
  connector?: string;
  tool?: string;
  chat_id?: string;
  request?: string;
  [k: string]: unknown;
}

// ---- System -----------------------------------------------------------------
export interface SystemInfo {
  brand: string;
  version: string;
  code_approval: string;
  learning_enabled: boolean;
  learning_interval_h: number;
  voice: boolean;
  voiceprint: boolean;
  web_search: boolean;
  image: boolean;
  docker: boolean;
}

export const hub = {
  // Models
  hardware: () => req<HardwareInfo>('/api/setup/hardware'),
  backends: () => req<BackendProbe>('/api/setup/backends'),
  setupConnectors: () => req<{ connectors: SetupConnector[] }>('/api/setup/connectors'),
  save: (body: SavePayload) =>
    req<{ ok?: boolean; error?: string; restart_required?: boolean }>('/api/setup/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  // Agent
  agentStatus: () => req<AgentStatus>('/api/hub/agent/status'),
  agentProvision: () =>
    req<{ ok: boolean; steps: { step: string; ok: boolean; detail: string }[]; detail: string }>(
      '/api/hub/agent/provision',
      { method: 'POST' },
    ),

  // Connectors
  connectors: () => req<{ connectors: HubConnector[] }>('/api/hub/connectors'),
  generate: (id: string, write: boolean) =>
    req<GenerateResult>(`/api/hub/connectors/${encodeURIComponent(id)}/generate?write=${write ? 1 : 0}`, {
      method: 'POST',
    }),
  newConnector: (body: NewConnectorBody) =>
    req<{ ok: boolean; path?: string; actions?: number; error?: string }>('/api/hub/connectors/new', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  probeConnector: (body: { url?: string; command?: string; token_env?: string }) =>
    req<ProbeResult>('/api/hub/connectors/probe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  // Model store
  models: () => req<ModelStore>('/api/hub/models'),
  pull: (role: string) =>
    req<{ ok: boolean; error?: string }>(`/api/hub/models/pull?role=${encodeURIComponent(role)}`, { method: 'POST' }),
  pullStatus: () => req<PullStatus>('/api/hub/models/pull/status'),
  bench: (prompt?: string) =>
    req<{ ok: boolean; error?: string }>('/api/hub/models/bench', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: prompt || undefined, max_tokens: 200 }),
    }),
  benchStatus: () => req<BenchStatus>('/api/hub/models/bench/status'),

  // Voice — raw fetch: enroll/test return structured {ok:false,error} bodies on
  // 4xx (deps missing, bad audio), which we surface instead of throwing.
  voiceStatus: () => req<VoiceStatus>('/api/hub/voice/status'),
  voiceEnroll: async (clips: Blob[]): Promise<EnrollResult> => {
    const fd = new FormData();
    clips.forEach((b, i) => fd.append('files', b, `clip${i}.webm`));
    const r = await fetch('/api/hub/voice/enroll', { method: 'POST', body: fd, credentials: 'same-origin' });
    return (await r.json()) as EnrollResult;
  },
  voiceTest: async (clip: Blob): Promise<{ ok: boolean; similarity?: number; windows?: number; error?: string }> => {
    const fd = new FormData();
    fd.append('file', clip, 'test.webm');
    const r = await fetch('/api/hub/voice/test', { method: 'POST', body: fd, credentials: 'same-origin' });
    return await r.json();
  },
  voiceThreshold: (value: number) =>
    req<{ ok: boolean; error?: string; restart_required?: boolean }>(
      `/api/hub/voice/threshold?value=${encodeURIComponent(value)}`,
      { method: 'POST' },
    ),

  // Cost & budgets
  cost: () => req<CostSettings>('/api/hub/cost'),
  saveCost: (body: Partial<CostSettings>) =>
    req<{ ok: boolean; error?: string }>('/api/hub/cost', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  // Flight recorder
  audit: (limit = 200, kind = '') =>
    req<{ events: AuditEvent[] }>(`/api/hub/audit?limit=${limit}${kind ? `&kind=${encodeURIComponent(kind)}` : ''}`),

  // Approvals
  approvals: () => req<{ pending: PendingApproval[] }>('/api/hub/approvals'),
  decideApproval: (id: string, approve: boolean) =>
    req<{ ok: boolean }>(`/api/hub/approvals/${encodeURIComponent(id)}?decision=${approve ? 'approve' : 'deny'}`, { method: 'POST' }),

  // System
  system: () => req<SystemInfo>('/api/hub/system'),
  setApproval: (mode: string) =>
    req<{ ok: boolean; error?: string; restart_required?: boolean }>(
      `/api/hub/system/approval?mode=${encodeURIComponent(mode)}`,
      { method: 'POST' },
    ),
};
