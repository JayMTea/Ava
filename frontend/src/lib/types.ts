// Shared types for the Ava web app. These mirror the JSON the FastAPI bridge
// returns, so components and the API client stay in sync.

export type Role = 'user' | 'assistant';

// One left-rail app, derived server-side from a connector's `ui:` block
// (GET /api/apps). `embed` selects how the shell renders it.
export interface AppEntry {
  id: string;
  label: string;
  icon?: string | null; // manifest ui.icon glyph name (null = stable auto-pick, see appIcon)
  color?: string | null; // manifest ui.color identity accent (null = auto)
  section: 'core' | 'apps' | string;
  order: number;
  embed: 'native' | 'iframe' | 'none';
  view?: string | null; // embed=native: key into the frontend component registry
  url?: string | null; // embed=iframe: same-origin proxy path (/apps/<id>/)
  has_api: boolean;
}

export interface HistoryEntry {
  role: Role;
  content: string;
}

export interface Attachment {
  id: string;
  filename: string;
  kind: 'image' | 'doc';
  url?: string;
  ocr?: boolean;
  chars?: number;
  error?: string;
}

export interface ModelInfo {
  id: string;
  label: string;
  model?: string;
}

export interface CotStep {
  kind: 'thinking' | 'text' | 'tool';
  text?: string;
  name?: string;
}

// WHOSE a model row is. The twin of ModelState: that one says whether a model is
// live, this one says whether it is Ava's. Closed vocabulary, mirrored from
// ava_bridge/hardware.py _RELATIONS and guarded by
// tests/test_model_state_vocabulary.py.
//
// The panel used to render all four identically, so a third-party ComfyUI
// holding 65 GB read exactly like Ava's own brain, and a correct, live list
// looked like stale junk.
export type ModelRelation =
  | 'brain'        // what Ava thinks with
  | 'configured'   // an inference engine the owner set up, that Ava can route to
  | 'app'          // belongs to a connected app, which declared it in `owns:`
  | 'foreign';     // other software on this machine — measured, not managed

export type ModelState =
  | 'resident'   // holding weights in memory right now
  | 'idle'       // engine up, has the model, not in memory (it loads on demand)
  | 'absent'     // engine up, does NOT have this model — never downloaded
  | 'offline'    // engine could not be reached
  | 'remote'     // runs somewhere else, so not in this box's memory
  | 'unknown';   // residency genuinely unobservable

export interface HardwareStats {
  gpu: {
    name: string | null;
    util: number | null;
    temp: number | null;
    power: number | null;
    mem_used_mb: number | null;
    mem_total_mb: number | null;
  };
  mem: {
    total_gb: number | null;
    used_gb: number | null;
    free_gb: number | null;
    used_pct: number | null;
  };
  disk: {
    total_gb: number | null;
    used_gb: number | null;
    free_gb: number | null;
    used_pct: number | null;
    /** Which volume this describes — AVA_HOME's, where models actually land. */
    path?: string;
    /** True when the reading is a slice of a machine Ava cannot see: a WSL2
     *  vhdx advertises a ~1 TB ceiling and its own emptiness regardless of how
     *  full the drive behind it is. Same vocabulary as the memory pool. */
    capped?: boolean;
    cap_kind?: string | null;
  };
  cpu: { util: number | null };
  models?: Array<{
    id: string;
    name: string;
    model: string;
    model_id?: string | null;
    backend?: string | null;
    memory_mb: number | null;
    memory_gb: number | null;
    gpu_util?: number | null;
    gpu_active?: boolean;
    pid: number | null;
    status: string;
    source: string;
    // What this row IS, as a machine token — the backend never sends copy for
    // it (see CLAUDE.md), so the wording below is ours and matches the "brain"
    // badge Setup → Agent uses. Exactly one row is ever the brain.
    role_key?: 'brain' | '';
    // OBSERVED liveness, closed vocabulary (ava_bridge/hardware.py _STATES).
    // "unknown" means we could not look, never "not loaded".
    state?: ModelState;
    // Whether `state: 'resident'` was READ or concluded. False for an engine
    // that exposes no residency endpoint (vLLM, llama.cpp, MLX): it loads one
    // model at boot and holds it, so being served IS being resident — a correct
    // conclusion, but not an observation, and "In memory" reads identically
    // either way. Same distinction AllocModel.measured draws for resident_gib.
    state_measured?: boolean;
    // WHOSE this row is (ava_bridge/hardware.py _RELATIONS). Derived from
    // `role_key` / `backend` / `app` — never a second guess at which row is the
    // brain, which only models.effective_brain() decides.
    relation?: ModelRelation;
    // The connected app that claimed this row via its `owns:` block, or "".
    // A bare connector id: appAccent()/appIcon()/appById() resolve it.
    app?: string | null;
    // True when this backend is served because nothing was configured (the env
    // AVA_BACKEND_URL), not because anyone chose it. Ava ships no default
    // model, so this is no longer ever a model Ava invented.
    implicit?: boolean;
    // False = it runs somewhere else, so this host's memory is not where to
    // look for it. Only the brain is shown when it is not local.
    local?: boolean;
    // How much of the model sits in VRAM (Ollama reports the split).
    vram_mb?: number | null;
    served?: string[];
    role?: string;
    cmd?: string;
    component_count?: number;
    in_memory?: boolean | null;
    components?: Array<{
      name: string;
      kind: string;
      kind_label?: string;
      path?: string | null;
      // true = observed resident; false = observed NOT resident (configured
      // only); null/absent = residency unknown (process not observable).
      in_memory?: boolean | null;
    }>;
  }>;
  ts: number;
}

export interface Preview {
  persona?: string;
  url: string;
  seed?: number | null;
  theme?: string | null;
}

// ---- Artifacts (Claude-style side panel) -----------------------------------
export interface WeatherDay {
  date: string;
  weekday: string;
  tmax: number | null;
  tmin: number | null;
  precip: number | null;
  code: number | null;
  desc: string;
}

export interface WeatherCurrent {
  temp: number | null;
  feels: number | null;
  humidity: number | null;
  wind: number | null;
  code: number | null;
  desc: string;
}

export interface WeatherArtifactData {
  type: 'weather';
  title: string;
  location: string;
  units: { temp: string; wind: string };
  current: WeatherCurrent;
  daily: WeatherDay[];
  hourly: { time: string; temp: number | null; code: number | null }[];
}

export type Artifact = WeatherArtifactData;

// ---- Turn polling ----------------------------------------------------------
export interface TurnStatus {
  id: string;
  status: 'running' | 'done' | 'error';
  steps?: CotStep[];
  tools_used?: string[];
  reply?: string | null;
  previews?: Preview[];
  artifact?: Artifact | null;
  model?: ModelInfo | null;
  ctx_tokens?: number | null;
  error?: string | null;
  // Machine-readable ("model_unknown", "inference_down") — drives the fix-it
  // link. turns.py has always sent it and /api/turn/<id> returns the turn dict
  // verbatim; this type not declaring it is why a failed turn rendered a bare
  // string with nowhere to go.
  error_code?: string;
  degraded?: boolean;
}

export interface ModelBackend {
  id: string;
  label: string;
  model: string;
  implicit?: boolean;   // built-in/env default served when nothing is configured
}

export interface ModelRoute {
  mode: string | null;
  backends: ModelBackend[];
  // The agent sandbox's model when that runtime is active — chat turns think
  // with THIS and bypass the router, so the picker only steers the fallback.
  agent_model?: string | null;
}

// ---- Chats -----------------------------------------------------------------
export interface ChatSummary {
  id: string;
  title?: string;
  updated?: number;
}

export interface ChatMessage {
  role: Role | 'assistant';
  content?: string;
  atts?: Attachment[];
  model?: ModelInfo | null;
  tools_used?: string[];
  steps?: CotStep[];
  error_code?: string; // machine-readable ("voice_off", "inference_down") — drives the fix-it link
}

export interface ChatDetail {
  id: string;
  title?: string;
  messages: ChatMessage[];
}

// ---- Voice (push-to-talk) --------------------------------------------------
export interface TalkResponse {
  accepted?: boolean;
  text?: string;
  reply?: string;
  tools_used?: string[];
  note?: string;
  error?: string;
  error_code?: string;  // machine-readable (e.g. "voice_off") — drives the fix-it link
  sim?: number | null;
  threshold?: number;
  audio?: string; // base64 WAV of Ava's spoken reply
  model?: ModelInfo | null;
}

// Connected-app data shapes moved to the optional overlay
// (frontend/src/overlay/lib/appTypes.ts) so the core carries no personal-app types.

// ---- Learning --------------------------------------------------------------
export type LearnContext = 'code' | 'chat';

export interface StagedChange {
  path: string;
  status?: 'M' | 'A' | 'D' | string;
  diff?: string;
}

export interface Proposal {
  id: string;
  title?: string;
  description?: string;
  why?: string;
  type?: string;
  risk?: string;
  effort?: string;
  status?: 'pending' | 'approved' | 'rejected' | 'completed' | string;
  feedback?: number | null;
  requires_approval?: boolean;
  project?: string;
  source?: string;
  requested_by?: string;
  completed_by?: string | null;
  approved_by?: string | null;
  completed_at?: string | null;
  applied?: boolean;
  applied_commit?: string;
  applied_branch?: string;
  staged_changes?: StagedChange[];
  paths?: string[];
  added?: number;
  removed?: number;
}

export interface LearningCycle {
  id: string;
  timestamp?: string;
  context?: LearnContext;
  patterns?: Record<string, unknown>;
  proposals?: Proposal[];
}

export interface LearningState {
  context: LearnContext;
  cycles: LearningCycle[];
  last_cycle?: string | null;
  enabled?: boolean;   // features.learning — off means no proposals will ever come
}

export interface LearningActionResult {
  ok: boolean;
  message?: string;
  error?: string;
  files?: string[];
  commit?: string;
  restart?: boolean;
}
