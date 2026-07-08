// Shared types for the Ava web app. These mirror the JSON the FastAPI bridge
// returns, so components and the API client stay in sync.

export type Role = 'user' | 'assistant';

// One left-rail app, derived server-side from a connector's `ui:` block
// (GET /api/apps). `embed` selects how the shell renders it.
export interface AppEntry {
  id: string;
  label: string;
  icon: string;
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

export interface ImageJob {
  id: string;
  prompt?: string;
  rewritten_prompt?: string;
  url?: string;
  status?: 'running' | 'done' | 'error';
  progress?: number;
  stage?: string;
  created?: number;
  updated?: number;
  cancelled?: boolean;
  error?: string;
}

export interface HardwareStats {
  gpu: {
    name: string | null;
    util: number | null;
    temp: number | null;
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
  };
  cpu: { util: number | null };
  models?: Array<{
    id: string;
    name: string;
    model: string;
    memory_mb: number | null;
    memory_gb: number | null;
    gpu_util?: number | null;
    gpu_active?: boolean;
    pid: number | null;
    status: string;
    source: string;
    role?: string;
    cmd?: string;
    component_count?: number;
    in_memory?: boolean;
    components?: Array<{
      name: string;
      kind: string;
      kind_label?: string;
      path?: string | null;
      in_memory?: boolean;
    }>;
  }>;
  jobs?: Array<{
    name: string;
    stage?: string | null;
    progress?: number | null;
    engine?: string;
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
  job?: ImageJob | null;
  previews?: Preview[];
  artifact?: Artifact | null;
  model?: ModelInfo | null;
  ctx_tokens?: number | null;
  error?: string | null;
  degraded?: boolean;
}

export interface ModelBackend {
  id: string;
  label: string;
  model: string;
}

export interface ModelRoute {
  mode: string | null;
  backends: ModelBackend[];
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
  image?: string;
  model?: ModelInfo | null;
  tools_used?: string[];
  steps?: CotStep[];
  url?: string;
  caption?: string;
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
  sim?: number | null;
  threshold?: number;
  audio?: string; // base64 WAV of Ava's spoken reply
  job?: ImageJob;
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
}

export interface LearningActionResult {
  ok: boolean;
  message?: string;
  error?: string;
  files?: string[];
  commit?: string;
  restart?: boolean;
}
