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
  actions?: { id: string; method: string; path: string; description?: string }[];
  mcp?: { url?: string; command?: string; token_env?: string };
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

  // Model store
  models: () => req<ModelStore>('/api/hub/models'),
  pull: (role: string) =>
    req<{ ok: boolean; error?: string }>(`/api/hub/models/pull?role=${encodeURIComponent(role)}`, { method: 'POST' }),
  pullStatus: () => req<PullStatus>('/api/hub/models/pull/status'),

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

  // System
  system: () => req<SystemInfo>('/api/hub/system'),
  setApproval: (mode: string) =>
    req<{ ok: boolean; error?: string; restart_required?: boolean }>(
      `/api/hub/system/approval?mode=${encodeURIComponent(mode)}`,
      { method: 'POST' },
    ),
};
