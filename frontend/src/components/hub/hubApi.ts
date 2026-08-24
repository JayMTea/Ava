// Typed client for the setup/onboarding Hub. Same-origin (session cookie sent
// automatically); a 401 bounces to the server-rendered /login. Reuses the shared
// `req` helper so auth + error handling match the rest of the app.
import { req } from '../../lib/api';
import type { ModelFit } from '../../lib/modelFit';

// ---- Models / inference -----------------------------------------------------
/** GET /api/setup/hardware — see setup_wizard.api_hardware.
 *
 * `platform` and the note were returned by the route from the start and declared
 * here by nothing, so the Apple-Silicon explanation the backend has always sent
 * could never render. Same drift as BackendProbe below, and the pool fields
 * below arrived the same way: the route has reported WHICH pool it is gating on
 * since the fit-honesty fix, while this interface still described a bare size —
 * so HardwarePanel could only print "No local GPU detected" on machines whose
 * card is simply invisible from inside a container.
 *
 * `note_code` names a situation, not a sentence: the wording is this layer's to
 * choose, and the wizard's one-line version and this panel's full version are
 * deliberately different (CLAUDE.md — backend returns facts).
 */
export interface HardwareInfo {
  fit_gb: number | null;
  source: string | null;
  tier: string;
  hint: string;
  gpu: string | null;
  platform: string | null;
  note_code: '' | 'apple-silicon' | 'container-no-gpu';
  /** vram = a dedicated accelerator pool; unified = one pool shared by CPU and
   *  GPU (Apple, GB10); system = plain RAM with no accelerator behind it. */
  pool_kind: 'vram' | 'unified' | 'system' | 'unknown';
  accelerated: boolean;
  /** False when no reader could run at all — the container case, distinct from
   *  "this machine genuinely has no accelerator". */
  accel_measurable: boolean;
  /** This pool is a slice of a bigger machine (a container limit, or the WSL2
   *  VM's default half of the host's RAM). */
  capped: boolean;
  cap_kind: 'cgroup' | 'wsl2-vm' | null;
  /** The pool size was STATED by the owner, not measured. `measured_gb` is what
   *  the box actually reported, kept so a surface can show both rather than
   *  passing a typed number off as a reading. */
  stated: boolean;
  measured_gb: number | null;
  /** "env" values cannot be edited from Setup — the process would keep ignoring
   *  ava.yaml and the owner would be changing a number that does nothing. */
  stated_source: '' | 'env' | 'config';
  /** Stated meaningfully MORE than the box measured: the tier goes up, the model
   *  does not fit, the kernel kills it on first load. */
  overstated: boolean;
}

/** GET/POST /api/hub/hardware/pool — the stated pool, and whether it is ours to set. */
export interface StatedPool {
  stated_gb: number | null;
  source: '' | 'env' | 'config';
  env_var: string;
  editable: boolean;
  min_gb: number;
  max_gb: number;
}
/** One declared model in the allocation report: what it holds, and what may be done.
 *
 * Every field is a FACT or a machine token — no sentence the backend wrote for a
 * person (CLAUDE.md). The wording for all of it lives in the component that renders
 * it, which is why `release_blocked` is a reason code rather than a message and
 * `modes` is a list rather than a label.
 *
 * The three-valued fields are the ones to be careful with. `resident: null` means
 * "we genuinely could not look", never "no" — the distinction `probe_resident` and
 * `modelState.ts` both exist to keep. `measured: false` means `resident_gib` is a
 * declared hint, not a reading, so it must never be rendered as a measurement.
 */
export interface AllocModel {
  id: string;
  label: string;
  driver: string;
  source: string;
  implicit: boolean;
  priority: string;
  pinned: boolean;
  local: boolean;
  observe_only: boolean;
  weight_gb: number | null;
  resident: boolean | null;
  resident_gib: number | null;
  measured: boolean;
  ready: boolean | null;
  detail: string;
  /** Which levers the driver offers, cheapest first. Empty = nothing to offer. */
  modes: string[];
  /** Why there is no lever, when there is none. */
  release_blocked: string | null;
  /** The engine reloads its own weights, so "bring it back" is not a start. */
  self_restoring: boolean;
  restore_kind: 'none' | 'self' | 'start';
  /** Live leases using it. Ava does not take memory from work in progress. */
  held_by: number;
  released_by_owner: boolean;
  released_by_us: boolean;
  released_at: number | null;
  released_gib: number | null;
  is_brain: boolean;
  problems: string[];
  notes: string[];
}

/** GET /api/hub/hardware/alloc — what is holding memory, and what Ava can free. */
export interface AllocReport {
  /** False when `alloc.enabled` is off. Not a 404, so the panel can say why. */
  enabled: boolean;
  actuating?: boolean;
  gating?: string;
  pool?: {
    free_gib: number | null; total_gib: number | null; source: string | null;
    baseline_gib: number | null; declared_gib: number | null;
    unknown_gib: number | null;
  };
  models: AllocModel[];
  declared_count: number;
  breaker?: { quiesced?: boolean; quiesce_reason?: string; models?: Record<string, {
    given_up?: boolean; fails?: number; reason?: string }> };
  job?: AllocJob;
}

/** The single in-flight release/restore. One slot for the whole surface. */
export interface AllocJob {
  status: 'idle' | 'running' | 'done' | 'error';
  model: string | null;
  verb: 'release' | 'restore' | null;
  log: string[];
  started_at: number | null;
  ended_at: number | null;
  result: {
    ok: boolean;
    code: string;
    detail?: string;
    mode?: string;
    /** MEASURED, and only ever present on a release that worked. Absent means the
     *  amount is unknown — which must read as "unknown", never as zero. */
    freed_gib?: number | null;
    free_before_gib?: number | null;
    free_after_gib?: number | null;
    measured?: boolean;
    held_by?: number;
    log?: string[];
  } | null;
}

/** Where an engine sits relative to Ava — not merely whether it answered.
 *
 * `host` is the load-bearing one: that engine runs on the machine itself,
 * OUTSIDE Ava's container, so it draws on memory and a GPU that
 * `/api/setup/hardware` cannot measure and must not size a recommendation from.
 */
export type EngineLocality = 'container' | 'compose' | 'host' | 'remote' | 'unknown';

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
  /** The engine's proper name from the registry — "vLLM", "LM Studio",
   *  "llama.cpp". Title-casing `engine` here would render "Vllm". */
  engine_label: string;
  note: string;        // "configured" | "from AVA_BACKEND_URL" | "compose service" | "local" | "on the host machine"
  up: boolean;
  locality: EngineLocality;
  /** Guessed at by Ava (a port sweep) rather than named by the owner. */
  blind: boolean;
}
export interface BackendProbe {
  backends: BackendCandidate[];
  any_up: boolean;
  router: boolean;
  /** Ava is inside a container, so its own loopback is not the user's machine. */
  in_container: boolean;
  /** The name this container can reach its host by, "" if there is none. Its
   *  presence is what makes "you may have bound the engine to loopback" a
   *  possible explanation rather than a guess. */
  host_gateway: string;
  /** Why the host could not be reached, asked only when nothing answered.
   *  `refused` = reachable, nothing listening on an address a container can use
   *  (the bind-address case). `dropped` = packets filtered, which no bind
   *  address fixes — Windows Defender on the WSL adapter looks like this. */
  host_reach: '' | 'refused' | 'dropped';
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
  /** What `models.effective_brain()` resolved — the ONE answer to "which model
   *  does Ava think with". Read this rather than re-deriving it from
   *  `available && sandbox_model`, which was a fourth independent derivation of
   *  the same question and free to disagree with the other three. */
  brain?: { source: string; model: string; label: string; engine: string;
            implicit: boolean } | null;
  /** 'local' | 'remote' — which machine cli/sandbox describe. */
  location?: string;
  /** How the runtime describes ITSELF — the words, not the config token in
   *  `runtime`. Hardcoding "NemoClaw" in panel copy told a `remote` or
   *  `direct` install about a runtime it is not running. */
  display_name?: string;
  blurb?: string;
  install_hint?: string | null;
  url?: string;
  error?: string;
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

// ---- Provisioning: what the sandbox is running vs what the repo declares -----
// The four states are the same vocabulary skills have always used, so a badge
// written for one domain reads correctly for all of them. `unknown` means "we
// could not look" — NOT "it is missing" — and never counts as pending.
export type DriftState = 'deployed' | 'stale' | 'undeployed' | 'unknown';
export type ProvisionScope = 'persona' | 'policies' | 'servers' | 'skills';

export interface ProvisionItem {
  scope: ProvisionScope;
  id: string;
  label: string;
  state: DriftState;
  source: 'registry' | 'probe' | 'manifest' | 'none';
  rel?: string;
  verified?: boolean | null;
  verify_detail?: string;
}

export interface ProvisionScopeState {
  state: DriftState;
  pending: number;
  source: string;
  counts: { deployed: number; stale: number; undeployed: number; unknown: number; total: number };
}

export interface ProvisionState {
  ok: boolean;
  state: DriftState;
  runtime: string;
  enabled: boolean;
  location: 'local' | 'remote';
  sandbox: {
    name: string | null;
    live: boolean;
    reason: string;
    rebuilt: boolean;
    model: string | null;
    versions: { nemoclaw?: string | null; openshell?: string | null; agent?: string | null };
  };
  run: { scope?: string; ended?: number; rc?: number } | null;
  scopes: Record<ProvisionScope, ProvisionScopeState>;
  items: ProvisionItem[];
  counts: { deployed: number; stale: number; undeployed: number; unknown: number; total: number };
  /** stale + undeployed. NEVER includes `unknown`. */
  pending: number;
  scopes_to_provision: ProvisionScope[];
}

export interface ProvisionStep {
  scope: string;
  id: string;
  ok: boolean;
  detail: string;
}

export interface ProvisionJob {
  status: 'idle' | 'running' | 'done' | 'error';
  id: string | null;
  scope: string | null;
  started_at: number | null;
  ended_at: number | null;
  rc: number | null;
  steps: ProvisionStep[];
  log: string[];
  seq: number;
  detail: string;
  /** false where the runtime cannot stream (remote): the view must not draw a
   *  checklist that never fills. */
  observable: boolean;
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
/** Whether an embedded app's UI gets its own browser origin.
 *
 *  With `apps.origin` unset — the default — an app Ava embeds is GENUINELY
 *  same-origin with the shell, so its JavaScript runs with the owner's session
 *  and can call `/api/hub/*`, including approving Ava's own consent prompts.
 *  `ok:false` carries the sentence the owner needs to read before handing an app
 *  a sidebar tile. See ava_bridge/apps_origin.py for why an Origin header check
 *  cannot substitute. */
export interface AppsOrigin { ok: boolean; origin: string | null; detail?: string }
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
  /** Will it actually run well here? Absent on an older bridge, which
   *  `fitLine()` reads as silence rather than as "fine". */
  fit?: ModelFit;
}
export interface ModelStore {
  roles: ModelRole[];
  detected_tier: string;
  available_gb: number | null;
  store: string;
}
export interface StoreModel {
  engine: string;
  id: string;
  path: string;
  size_gb: number | null;
  in_use: boolean;
  /** What is holding it, in the owner's terms. Empty = safe to delete. */
  held_by: string[];
}
export interface ModelStoreList {
  models: StoreModel[];
  total_gb: number;
  store: string;
}
export interface SwapStatus {
  status: 'idle' | 'running' | 'done' | 'error';
  model: string | null;
  error: string | null;
  log: string[];
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
  /** The files are written and the sandbox half was handed to the shared
   *  single-slot provisioning job — the same one "Apply to the agent" uses.
   *  Wait on it with `attachToProvisionJob()`; do NOT block on this request,
   *  which is exactly what the ten-minute synchronous POST used to do. */
  running?: boolean;
  job_id?: string;
  steps?: { step: string; ok: boolean; detail: string }[];
  detail?: string;
  error?: string;
  /** `provision_running` — another run holds the slot (HTTP 409). */
  error_code?: string;
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
export interface ConnectorLive {
  ok: boolean;
  transport: string;
  verified: boolean;
  tools: number | null;
  error: string | null;
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
  // The app answered 401/403: it exists and wants credentials. Distinct from
  // "it has no tools", which is what every auth failure used to look like.
  needs_auth?: boolean;
  // Why each discovery step gave up, in order. The probe tries six things and
  // used to swallow all six, so a TLS error and "not that kind of app" produced
  // the same sentence.
  tried?: string[];
}

// ---- Cost & budgets ---------------------------------------------------------
export interface PersonaPreset { id: string; label: string; text: string }
export interface PersonaFormatChoice { id: string; label: string; hint: string }

export interface BrandContrast {
  ok: boolean;
  blocking: string[];
  warnings: string[];
  ratios: Record<string, number>;
  /** A same-hue colour that passes, offered as a one-click fix on a refusal. */
  suggest: string;
}

export interface BrandingSettings {
  name: string;
  tagline: string;
  /** '' means Ava's shipped value — NOT "unset, pick a fallback". */
  accent: string;
  accent_light: string;
  chrome: string;
  public: boolean;
  accessibility_check: boolean;
  branded: boolean;
  /** Per slot: what is in force (`set`/`url`), whether an image is parked by a
   *  toggle and can be switched back to (`stashed`), and what Ava's own default
   *  looks like (`default_url`). `set: false` alone cannot tell "never uploaded"
   *  from "uploaded, currently showing Ava's" — those are different controls. */
  assets: Record<string, {
    set: boolean; url: string | null; stashed: boolean; default_url: string;
  }>;
  /** From the server so the panel never hardcodes a value the backend enforces. */
  defaults: {
    name: string; tagline: string; accent: string; accent_light: string;
    chrome_dark: string; chrome_light: string;
  };
  limits: {
    name_max: number; tagline_max: number; slots: string[];
    text_on_accent_min: number; accent_on_canvas_min: number;
  };
  contrast: BrandContrast;
  /** Present keys are outranking ava.yaml, so a save will not bite. */
  env_overrides: Partial<Record<string, string>>;
  config_error?: string;
}

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
  /** Who is blocked on this. `connector` is one of Ava's own bridge calls;
   *  `agent` is a command the agent's gateway parked for a human. Both are the
   *  same kind of thing — a call waiting on a person — so they share one
   *  banner rather than giving the operator a second place to look. */
  source?: 'connector' | 'agent';
  /** Which decisions THIS row accepts. Gateway rows state their own; hardcoding
   *  the three breaks the day a build offers a different set. */
  decisions?: ('approve' | 'always' | 'deny')[];
  /** The gateway's own warning text about the command, when it has one. */
  warning?: string | null;
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
  voice: boolean;
  voiceprint: boolean;
  web_search: boolean;
  // The capability registry (ava_bridge/features.py) — the Optional features
  // panel renders straight from this, so new capabilities appear automatically.
  features: FeatureEntry[];
  docker: boolean;
  retention_days: number;        // 0 == keep forever
  retention_choices: number[];
  // Editable keys currently shadowed by env vars (name -> env var). A yaml
  // write "succeeds" but the env value wins again on the next boot.
  env_overrides?: Record<string, string>;
  // '' when ava.yaml parses. When it does NOT, every value above is a DEFAULT
  // rather than the owner's setting, and no save will be accepted — so the panel
  // must say so instead of letting them toggle things that cannot persist. The
  // backend has returned these two since hub/system.py was written; the interface
  // simply never declared them, so the signal was dropped on the floor.
  config_error?: string;
  config_path?: string;
}

export const hub = {
  // First-run walkthrough. State lives server-side because Ava is single-user
  // but multi-DEVICE: a localStorage flag would replay the whole thing on the
  // owner's phone and after any cache clear.
  // Can Ava answer right now? no-store because this is install truth, and a
  // cached "yes" outliving a broken engine is the failure it exists to catch.
  inference: () => req<{ ok: boolean; code?: string; detail?: string;
                         model?: string; engine?: string }>(
    '/api/hub/agent/inference', { cache: 'no-store' }),
  tour: () => req<{ seen: string[]; pages: string[] }>('/api/hub/tour', { cache: 'no-store' }),
  tourSeen: (page: string) =>
    req<{ ok: boolean; seen: string[] }>(
      `/api/hub/tour/seen?page=${encodeURIComponent(page)}`, { method: 'POST' }),
  tourReset: () => req<{ ok: boolean; seen: string[] }>('/api/hub/tour/reset', { method: 'POST' }),

  // Models
  hardware: () => req<HardwareInfo>('/api/setup/hardware'),
  statedPool: () => req<StatedPool>('/api/hub/hardware/pool'),
  setStatedPool: (gb: number | null) =>
    req<{ ok: boolean; error?: string; stated_gb?: number | null }>(
      '/api/hub/hardware/pool', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ gb }),
      }),

  // Freeing model memory. Every mutation is POST, never GET: the session cookie is
  // samesite=lax, which DOES ride a top-level GET navigation, so a GET that freed
  // memory would be a one-click drive-by that takes Ava's brain down.
  alloc: () => req<AllocReport>('/api/hub/hardware/alloc', { cache: 'no-store' }),
  allocJob: () => req<AllocJob>('/api/hub/hardware/alloc/job', { cache: 'no-store' }),
  allocRelease: (id: string, mode?: string) =>
    req<{ ok: boolean; code?: string; job?: AllocJob }>(
      `/api/hub/hardware/alloc/${encodeURIComponent(id)}/release`
      + (mode ? `?mode=${encodeURIComponent(mode)}` : ''), { method: 'POST' }),
  allocRestore: (id: string) =>
    req<{ ok: boolean; code?: string; job?: AllocJob }>(
      `/api/hub/hardware/alloc/${encodeURIComponent(id)}/restore`, { method: 'POST' }),
  allocReset: (id: string) =>
    req<{ ok: boolean }>(
      `/api/hub/hardware/alloc/${encodeURIComponent(id)}/reset`, { method: 'POST' }),
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
  agentGateway: () => req<{
    ok: boolean;
    configured: boolean;
    source: string;
    url: string;
    allow_remote: boolean;
    device_auth: { known: boolean; change_with: string; sandbox: string };
    /** Who the gateway says it is. `null` when the configured runtime has no
     *  identity to report — rendered as ABSENT rows rather than "unknown"
     *  ones, because not-applicable and not-known are different answers. */
    identity: {
      device_id?: string;
      agent_id?: string;
      agent_name?: string;
      paired?: number | null;
      pending?: number | null;
      agents?: number;
    } | null;
  }>('/api/hub/agent/gateway'),
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
  // Non-blocking: returns a job id and the run streams into provisionJob().
  // `steps`/`detail` are still in the response so an older committed dist against
  // a newer bridge renders something rather than crashing on a missing key.
  agentProvision: (scope: ProvisionScope | 'all' = 'all') =>
    req<{
      ok: boolean; job_id?: string; scope?: string; status?: string;
      steps?: { step: string; ok: boolean; detail: string }[]; detail?: string;
      error?: string; error_code?: string;
    }>(`/api/hub/agent/provision?scope=${scope}`, { method: 'POST' }),

  provisionState: () =>
    req<ProvisionState>('/api/hub/agent/provision/state', { cache: 'no-store' }),

  provisionJob: (since = 0) =>
    req<ProvisionJob>(`/api/hub/agent/provision/status?since=${since}`,
      { cache: 'no-store' }),

  // Connectors
  connectors: () =>
    req<{ connectors: HubConnector[]; errors?: ConnectorLoadError[];
          apps_origin?: AppsOrigin }>('/api/hub/connectors'),
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
    req<{ ok: boolean; path?: string; actions?: number; auth_env?: string | null;
          auth_saved?: boolean; error?: string;
          // The connector EXISTS, but something about it did not check out: a
          // probe URL that will not dial, an access tier the loader rejected, an
          // app that was unreachable when its tools were read. The route used to
          // discard all of this and answer a flat `ok: true`.
          warnings?: string[] }>('/api/hub/connectors/new', {
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
  // `running: true` + `job_id` means the sandbox half was handed to the shared
  // single-slot provisioning job; wait on it with attachToProvisionJob(). A 409
  // means another run already holds the slot.
  deployConnector: (id: string) =>
    req<DeployResult>(`/api/hub/connectors/${encodeURIComponent(id)}/deploy`, { method: 'POST' }),
  deleteConnector: (id: string) =>
    req<{ ok: boolean; error?: string }>(
      `/api/hub/connectors/${encodeURIComponent(id)}/delete`, { method: 'POST' }),
  /** Actually talk to the app right now. The row's transport chip is drawn from
   *  the manifest, which can only say what a connector CLAIMS; this is the one
   *  call that round-trips (MCP initialize + tools/list, or a GET on the
   *  ava-tools/1 facade). `verified: false` means nothing was hand-shaken — a
   *  `rest` connector has no handshake — and the count is declared, not seen. */
  connectorLive: (id: string) =>
    req<ConnectorLive>(`/api/hub/connectors/${encodeURIComponent(id)}/live`),

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
  store: () => req<ModelStoreList>('/api/hub/models/store'),
  storeDelete: (engine: string, id: string, force = false) =>
    req<{ ok: boolean; error?: string; held_by?: string[]; freed_gb?: number | null }>(
      '/api/hub/models/store/delete',
      { method: 'POST', body: JSON.stringify({ engine, id, force }) }),
  storeBrain: (engine: string, id: string) =>
    req<{ ok: boolean; error?: string }>('/api/hub/models/store/brain',
      { method: 'POST', body: JSON.stringify({ engine, id }) }),
  storeBrainStatus: () => req<SwapStatus>('/api/hub/models/store/brain/status'),
  pullModel: (engine: string, id: string) =>
    req<{ ok: boolean; error?: string }>(
      `/api/hub/models/pull?engine=${encodeURIComponent(engine)}&id=${encodeURIComponent(id)}`,
      { method: 'POST' }),
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

  // Branding — how Ava LOOKS (name, colour, logo). Free, ungated, and
  // deliberately so: see ava_bridge/hub/branding.py.
  branding: () => req<BrandingSettings>('/api/hub/branding'),
  saveBranding: (body: Partial<Pick<BrandingSettings,
    'name' | 'tagline' | 'accent' | 'accent_light' | 'chrome' | 'public' | 'accessibility_check'>>) =>
    req<{
      ok: boolean; error?: string; error_code?: string;
      restart_required?: boolean; reprovision_required?: boolean;
      contrast?: BrandContrast;
    }>('/api/hub/branding', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  uploadBrandAsset: (slot: string, file: File) => {
    const fd = new FormData();
    fd.append('file', file);
    // No Content-Type header: the browser must set the multipart boundary.
    return req<{ ok: boolean; slot: string; url: string }>(
      `/api/hub/branding/asset/${encodeURIComponent(slot)}`, { method: 'POST', body: fd });
  },
  clearBrandAsset: (slot: string) =>
    req<{ ok: boolean }>(`/api/hub/branding/asset/${encodeURIComponent(slot)}`,
      { method: 'DELETE' }),

  /** Switch one slot between Ava's shipped default and the owner's own image.
   *  Non-destructive both ways — the image is parked, not deleted, which is
   *  what makes this a toggle and clearBrandAsset the separate delete. */
  setBrandAssetSource: (slot: string, source: 'default' | 'custom') =>
    req<{ ok: boolean; slot: string; source: string; set: boolean;
          stashed: boolean; url: string | null }>(
      `/api/hub/branding/asset/${encodeURIComponent(slot)}/source`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source }),
      }),

  importBrandPack: (file: File) => {
    const fd = new FormData();
    fd.append('file', file);
    return req<{ ok: boolean; applied: string[]; assets: string[] }>(
      '/api/hub/branding/import', { method: 'POST', body: fd });
  },

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
  setRetention: (days: number) =>
    req<{ ok: boolean; error?: string; restart_required?: boolean }>(
      `/api/hub/system/retention?days=${days}`,
      { method: 'POST' },
    ),
};
