// Typed client for the setup/onboarding Hub. Same-origin (session cookie sent
// automatically); a 401 bounces to the server-rendered /login. Reuses the shared
// `req` helper so auth + error handling match the rest of the app.
import { req } from '../../lib/api';

// ---- Models / inference -----------------------------------------------------
/** GET /api/setup/hardware — see setup_wizard.api_hardware.
 *
 * `platform` and `note` were returned by the route from the start and declared
 * here by nothing, so the Apple-Silicon explanation the backend has always sent
 * could never render. Same drift as BackendProbe below.
 */
export interface HardwareInfo {
  fit_gb: number | null;
  source: string | null;
  tier: string;
  hint: string;
  gpu: string | null;
  platform: string | null;
  note: string;
}
/** GET /api/setup/backends — every candidate endpoint, probed right now.
 *
 * This used to declare `{vllm, ollama, router}`, which the route has never
 * returned. Both fields read `undefined` on every response, so "No local engine
 * detected" rendered permanently — including for people whose engine was
 * answering — and Overview's "(engine up)" was unreachable. Keep this in step
 * with setup_wizard.api_backends.
 */
export interface BackendCandidate {
  id: string;
  base_url: string;
  engine: string;      // vllm | ollama | openai | …
  note: string;        // "configured" | "from AVA_BACKEND_URL" | "compose service" | "local"
  up: boolean;
}
export interface BackendProbe {
  backends: BackendCandidate[];
  any_up: boolean;
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
  enabled: boolean;      // agent.enabled / AVA_AGENT_ENABLED — the on/off switch
  runtime: string;
  required: boolean;
  cli: string | null;
  sandbox: string | null;
  sandbox_exists: boolean | null;
  // What the agent actually thinks with (set by `nemoclaw onboard`, not by
  // ava.yaml's inference block) — the Hub's brain panel pins this as the
  // effective brain while the agent is active.
  sandbox_model: string | null;
  sandbox_provider: string | null;
  enabled_env_override?: string | null;  // e.g. "AVA_AGENT_ENABLED" when env-forced
  health: unknown;
  tools: boolean;
}

// A skill = one SKILL.md capability file the agent runtime loads. Auto-
// discovered from the filesystem (agent/skills + overlay), so adding a folder
// surfaces it here with no registration. `deployed` is the honest per-skill
// state: whether it's actually live in the sandbox vs newly added in the repo.
export interface Skill {
  id: string;
  title: string;
  summary: string;
  description: string;
  category: string | null;
  icon: string | null;
  app?: string | null; // connector id the skill drives (SKILL.md `app:`) —
  // needed when tool names are discovered dynamically and carry no prefix
  tools: string[];
  source: 'core' | 'overlay';
  deployed: 'deployed' | 'stale' | 'undeployed' | 'unknown';
}

export interface SkillList {
  skills: Skill[];
  errors: { id: string; path: string; error: string }[];
  summary: { total: number; deployed: number; stale: number; unknown: number };
  // Owner-owned category order; also registers created-but-still-empty
  // categories (ava.yaml skills.category_order).
  category_order?: string[];
}

// ---- Connectors (Hub view) --------------------------------------------------
export interface HubConnector {
  id: string;
  label: string;
  kind: string;
  actions: number;
  mcp: boolean;
  discover?: boolean;  // dynamic tool facade (GET /tools + POST /call)
  app?: boolean;       // has a ui: block — an embedded APP tile
  // HOW this connector's tools reach Ava, classified server-side by
  // connectors.transport(). The UI renders this verbatim and never re-derives it
  // — inferring "MCP" from `mcp || discover || actions > 0` labelled every row
  // with tools as MCP, which made the badge meaningless. See TRANSPORT_LABEL.
  transport?: 'mcp' | 'discover' | 'rest' | 'none';
  has_policy: boolean;
  has_tools: boolean;
  renders_policy: boolean;
  enabled: boolean;
  builtin?: boolean;   // shipped in the repo — read-only (no edit/disable/delete)
  icon?: string | null;  // manifest ui.icon (null = stable auto-pick)
  color?: string | null; // manifest ui.color (null = stable auto-pick)
  auth_env?: string | null;  // env-var NAME the app authenticates with (null = no auth)
  auth_set?: boolean;        // a credential is available (real env var OR saved once)
  auth_stored?: boolean;     // a value is saved in Ava's secret store (so it can be cleared)
}
export interface ConnectorLoadError { id: string; path: string; error: string }
export interface ManifestResult { ok: boolean; yaml?: string; editable?: boolean; error?: string }
export interface GenerateResult {
  ok: boolean;
  policy?: string; // rendered YAML preview
  tools?: { name: string; source: string }[];
  wrote?: string[];
  error?: string;
}

// ---- Inference backends (the multi-model "brain" manager) -------------------
export interface Backend {
  id: string;
  engine: string;
  base_url: string;
  model: string;
  label: string;
  local: boolean;
  is_brain: boolean;
  is_primary: boolean;
  has_key: boolean;
}
export interface BackendList {
  backends: Backend[];
  brain: string | null;
  primary: string | null;
}
export interface BackendTestResult {
  ok: boolean;
  ms?: number;
  reply?: string;
  status?: number;
  error?: string;
}
export interface SaveBackendBody {
  id: string;
  engine: string;
  base_url: string;
  model: string;
  api_key?: string;
  make_brain?: boolean;
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
  result: {
    prompt?: string; results: BenchResult[]; winner?: string | null; error?: string;
    backend_count?: number; pending?: number;
  } | null;
}

// ---- Voice ------------------------------------------------------------------
export interface VoiceStatus {
  enabled: boolean;
  deps_ok: boolean;
  deps_error: string;
  enrolled: boolean;
  threshold: number;
}
/** The receipt from destroying a voiceprint. Absolute paths on purpose: the owner
 *  should be able to `test -f` each one instead of taking the UI's word for it. */
export interface VoiceDeleteReceipt {
  ok: boolean;
  error?: string;
  digest_before: string;
  removed: string[];
  absent: string[];
  failed?: { path: string; error: string }[];
  in_memory_evicted: { voiceprint: boolean; verifier: boolean };
  threshold_reset_from?: number;
  /** What was deliberately KEPT, and why. Shown, not hidden — a receipt claiming
   *  more than it did is the failure mode for a destruction claim. */
  not_destroyed: Record<string, string>;
  enroll_files_kept: string[];
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
  token_env?: string;   // the NAME of the env var the app authenticates with (optional)
  token_value?: string; // the actual token — saved once to Ava's secret store, never the manifest
  confirm?: boolean;
  role?: string;      // 'device' — enables the push flow + Devices grouping
  ingest?: boolean;   // let the app push readings/events with its ingest token
  ui?: boolean;       // write the embedded-app ui: block (sidebar tile + iframe proxy)
  ui_url?: string;    // split-container apps: the UI lives at a different address
  actions?: { id: string; method: string; path: string; description?: string; confirm?: boolean; access?: string }[];
  mcp?: { url?: string; command?: string; token_env?: string; sandbox?: string };
  discover?: { base?: string; list?: string; call?: string; token_env?: string };
}
export interface IngestToken {
  ok: boolean;
  token?: string;
  enabled?: boolean;
  url?: string;       // the push endpoint, e.g. /api/connectors/<id>/events
  error?: string;
}
export interface DeployResult {
  ok: boolean;
  deployed?: boolean;
  steps?: { step: string; ok: boolean; detail: string }[];
  detail?: string;
  error?: string;
}
export interface DeviceEvent {
  ts: number;
  cid: string;
  type: string;
  name: string;
  value?: number | string;
  unit?: string;
  message?: string;
  severity?: string;
}
export interface ProbeResult {
  ok: boolean;
  kind?: 'mcp' | 'discover' | 'rest' | 'unknown';
  transport?: string;
  tools?: { name: string; description: string }[];
  // Pre-filled from the app's OpenAPI/Swagger spec (a plain web app is zero-config).
  actions?: { id: string; method: string; path: string; description?: string; confirm?: boolean; access?: string }[];
  // The app serves its own web UI — offer the embedded sidebar tile (ui.embed: iframe).
  has_ui?: boolean;
  // Self-described via /.well-known/ava.json — prefill the connect form.
  label?: string;
  health?: string;
  discover?: { list: string; call: string };
  detail?: string;
  error?: string;
}

// ---- Cost & budgets ---------------------------------------------------------
export interface PersonaPreset { id: string; label: string; text: string }
export interface PersonaFormatChoice { id: string; label: string; hint: string }

export interface PersonaSettings {
  /** Empty on a fresh install — the shipped prompt carries no personality. */
  style: string;
  format: string;
  adult: boolean;
  /** Starting points only; the panel saves resolved text, never a preset id. */
  presets: PersonaPreset[];
  format_choices: PersonaFormatChoice[];
  style_max: number;
  /** Present keys are outranking ava.yaml, so a save will not bite. */
  env_overrides: Partial<Record<'style' | 'format' | 'adult', string>>;
  config_error: string;
}

export interface CostSettings {
  electricity_rate_per_kwh: number;
  currency: string;
  nominal_gpu_watts: number | null;
  budgets: { daily_usd: number | null; monthly_usd: number | null; daily_kwh: number | null };
  daily_spend_usd: number;
  /** Null when no wattage is available for this platform. */
  daily_energy_kwh: number | null;
  power_source: 'sampled' | 'declared' | 'platform-nominal' | null;
  power_measured: boolean;
}

// ---- JIT permission sheet (connector settings) --------------------------------
export interface GrantAction {
  id: string;
  access: 'read' | 'write' | 'destructive' | 'physical';
  capability: string;
  method: string;
  path: string;
  description: string;
  granted: boolean;
  grantable: boolean;
}

// ---- Approvals (human-in-the-loop) ------------------------------------------
export interface PendingApproval {
  id: string;
  connector: string;
  action: string;
  args: Record<string, string>;
  ts: number;
  // JIT consent: write-tier prompts may offer "Always allow"; destructive ones may not.
  grantable?: boolean;
  access?: 'read' | 'write' | 'destructive' | 'physical';
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

// ---- Memory (governed long-term store) ---------------------------------------
export interface MemoryItem {
  id: number;
  kind: 'fact' | 'doc';
  source: string;      // 'distilled' | 'manual' | 'upload:<filename>'
  text: string;
  created: number;
  updated: number;
  pinned: boolean;
  meta: Record<string, unknown>;
}
export interface MemoryCounts {
  facts: number;
  doc_chunks: number;
  total: number;
}

// ---- System -----------------------------------------------------------------
export interface FeatureEntry {
  key: string;      // features.<key> in ava.yaml
  label: string;
  sub: string;
  enabled: boolean;
}

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
  // The capability registry (ava_bridge/features.py) — the Optional features
  // panel renders straight from this, so new capabilities appear automatically.
  features: FeatureEntry[];
  docker: boolean;
  retention_days: number;        // 0 == keep forever
  retention_choices: number[];
  // Editable keys currently shadowed by env vars (name -> env var). A yaml
  // write "succeeds" but the env value wins again on the next boot.
  env_overrides?: Record<string, string>;
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
  agentSkills: () => req<SkillList>('/api/hub/agent/skills'),
  agentSkill: (id: string) =>
    req<{ id: string; title: string; body: string }>(`/api/hub/agent/skills/${encodeURIComponent(id)}`),
  setSkillCategory: (id: string, category: string | null) =>
    req<{ ok: boolean; error?: string }>(
      `/api/hub/agent/skills/${encodeURIComponent(id)}/category`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category }),
      }),
  renameSkillCategory: (from: string, to: string) =>
    req<{ ok: boolean; renamed?: number; error?: string }>(
      '/api/hub/agent/skills/categories/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ from, to }),
      }),
  setSkillCategoryOrder: (order: string[]) =>
    req<{ ok: boolean; order?: string[]; error?: string }>(
      '/api/hub/agent/skills/categories/order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order }),
      }),
  createSkillCategory: (name: string) =>
    req<{ ok: boolean; error?: string }>('/api/hub/agent/skills/categories/new', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }),
  deleteSkillCategory: (name: string) =>
    req<{ ok: boolean; error?: string }>('/api/hub/agent/skills/categories/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }),
  agentProvision: () =>
    req<{ ok: boolean; steps: { step: string; ok: boolean; detail: string }[]; detail: string }>(
      '/api/hub/agent/provision',
      { method: 'POST' },
    ),

  // Connectors
  connectors: () =>
    req<{ connectors: HubConnector[]; errors?: ConnectorLoadError[] }>('/api/hub/connectors'),
  setConnectorEnabled: (id: string, enabled: boolean) =>
    req<{ ok: boolean; enabled?: boolean; error?: string }>(
      `/api/hub/connectors/${encodeURIComponent(id)}/enabled`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      }),
  // Rail identity. Only the keys passed are touched; null clears one back to
  // the stable auto-pick (see lib/appColor).
  setAppearance: (id: string, patch: { icon?: string | null; color?: string | null }) =>
    req<{ ok: boolean; icon?: string | null; color?: string | null; error?: string }>(
      `/api/hub/connectors/${encodeURIComponent(id)}/appearance`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      }),
  getManifest: (id: string) =>
    req<ManifestResult>(`/api/hub/connectors/${encodeURIComponent(id)}/manifest`),
  saveManifest: (id: string, yaml: string) =>
    req<{ ok: boolean; error?: string }>(
      `/api/hub/connectors/${encodeURIComponent(id)}/manifest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ yaml }),
      }),
  generate: (id: string, write: boolean) =>
    req<GenerateResult>(`/api/hub/connectors/${encodeURIComponent(id)}/generate?write=${write ? 1 : 0}`, {
      method: 'POST',
    }),
  newConnector: (body: NewConnectorBody) =>
    req<{ ok: boolean; path?: string; actions?: number; auth_env?: string | null; auth_saved?: boolean; error?: string }>('/api/hub/connectors/new', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  probeConnector: (body: { url?: string; command?: string; token_env?: string;
                          token_value?: string; sandbox?: string;
                          allow_unsandboxed?: boolean }) =>
    req<ProbeResult>('/api/hub/connectors/probe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  // Save (or clear, with value: '') a connected app's credential VALUE so
  // redeploy never re-prompts. Keyed by the manifest's token_env NAME; the value
  // goes to Ava's server-side secret store, never the manifest or the agent.
  setConnectorSecret: (id: string, value: string) =>
    req<{ ok: boolean; auth_env?: string; auth_set?: boolean; auth_stored?: boolean; error?: string }>(
      `/api/hub/connectors/${encodeURIComponent(id)}/secret`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value }),
      }),
  ingestToken: (id: string) =>
    req<IngestToken>(`/api/hub/connectors/${encodeURIComponent(id)}/ingest-token`),
  lastEvent: (id: string) =>
    req<{ ok: boolean; event: DeviceEvent | null }>(
      `/api/hub/connectors/${encodeURIComponent(id)}/last-event`),
  deployConnector: (id: string) =>
    req<DeployResult>(`/api/hub/connectors/${encodeURIComponent(id)}/deploy`, { method: 'POST' }),
  deleteConnector: (id: string) =>
    req<{ ok: boolean; error?: string }>(
      `/api/hub/connectors/${encodeURIComponent(id)}/delete`, { method: 'POST' }),

  // Inference backends — the multi-model brain manager
  backendList: () => req<BackendList>('/api/hub/models/backends'),
  backendSave: (b: SaveBackendBody) =>
    req<{ ok: boolean; id?: string; error?: string; restart_required?: boolean }>(
      '/api/hub/models/backends', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(b),
      }),
  backendTest: (b: { id?: string; base_url: string; model: string; api_key?: string }) =>
    req<BackendTestResult>('/api/hub/models/backends/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(b),
    }),
  backendBrain: (id: string) =>
    req<{ ok: boolean; brain?: string; error?: string; restart_required?: boolean }>(
      `/api/hub/models/backends/${encodeURIComponent(id)}/brain`, { method: 'POST' }),
  backendDelete: (id: string) =>
    req<{ ok: boolean; error?: string; restart_required?: boolean }>(
      `/api/hub/models/backends/${encodeURIComponent(id)}/delete`, { method: 'POST' }),

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
  /** Destroy the enrolled voiceprint. Returns a receipt of absolute paths so the
   *  owner can verify by hand rather than trusting `enrolled: false`. */
  voiceDelete: () =>
    req<VoiceDeleteReceipt>('/api/hub/voice/delete', { method: 'POST' }),

  // Persona — how Ava talks (empty by default; see ava_bridge/hub/persona.py)
  persona: () => req<PersonaSettings>('/api/hub/persona'),
  savePersona: (body: { style?: string; format?: string }) =>
    req<{ ok: boolean; error?: string }>('/api/hub/persona', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

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

  // Approvals — decision: 'approve' (once) | 'always' (approve + durable grant) | 'deny'
  approvals: () => req<{ pending: PendingApproval[] }>('/api/hub/approvals'),
  decideApproval: (id: string, decision: 'approve' | 'always' | 'deny') =>
    req<{ ok: boolean }>(`/api/hub/approvals/${encodeURIComponent(id)}?decision=${decision}`, { method: 'POST' }),
  connectorGrants: (cid: string) =>
    req<{ grants: Record<string, { granted: string; by: string }>; actions: GrantAction[] }>(
      `/api/hub/connectors/${encodeURIComponent(cid)}/grants`),
  grantAction: (cid: string, action: string) =>
    req<{ ok: boolean; error?: string }>(`/api/hub/connectors/${encodeURIComponent(cid)}/grants/${encodeURIComponent(action)}`,
      { method: 'POST' }),
  revokeGrant: (cid: string, action: string) =>
    req<{ ok: boolean }>(`/api/hub/connectors/${encodeURIComponent(cid)}/grants/${encodeURIComponent(action)}`,
      { method: 'DELETE' }),

  // Memory
  memory: (q = '', kind = '', limit = 200) =>
    req<{ items: MemoryItem[]; counts: MemoryCounts; enabled: boolean }>(
      `/api/hub/memory?limit=${limit}${q ? `&q=${encodeURIComponent(q)}` : ''}${kind ? `&kind=${encodeURIComponent(kind)}` : ''}`),
  addMemory: (text: string) =>
    req<{ ok: boolean; id?: number; error?: string }>('/api/hub/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    }),
  updateMemory: (id: number, patch: { text?: string; pinned?: boolean }) =>
    req<{ ok: boolean; error?: string }>(`/api/hub/memory/${id}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  deleteMemory: (id: number) =>
    req<{ ok: boolean; error?: string }>(`/api/hub/memory/${id}/delete`, { method: 'POST' }),

  // System
  system: () => req<SystemInfo>('/api/hub/system'),
  setApproval: (mode: string) =>
    req<{ ok: boolean; error?: string; restart_required?: boolean }>(
      `/api/hub/system/approval?mode=${encodeURIComponent(mode)}`,
      { method: 'POST' },
    ),
  setRetention: (days: number) =>
    req<{ ok: boolean; error?: string; restart_required?: boolean }>(
      `/api/hub/system/retention?days=${days}`,
      { method: 'POST' },
    ),
};
