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

  // System
  system: () => req<SystemInfo>('/api/hub/system'),
  setApproval: (mode: string) =>
    req<{ ok: boolean; error?: string; restart_required?: boolean }>(
      `/api/hub/system/approval?mode=${encodeURIComponent(mode)}`,
      { method: 'POST' },
    ),
};
