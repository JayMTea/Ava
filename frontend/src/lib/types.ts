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

/* ── Domains ──────────────────────────────────────────────────────────────
 * The wire shapes of /api/domains and /api/domains/{realm}/{domain}.
 *
 * `state` and `provenance` are ORTHOGONAL and both optional-by-absence:
 * `state` says whether a number arrived, `provenance` how good the one that
 * did is. `provenance` is null on EVERY absence — a reading that does not
 * exist has no quality to describe, and leaving a value there is how a gap
 * gets counted as a good measurement.
 *
 * No axis VALUES appear in this file. The vocabulary is the owner's and
 * arrives at runtime in `axes`; the product defines none of it.
 */
export type ObsState = 'ok' | 'insufficient' | 'unavailable' | 'no_source';
export type Provenance = 'measured' | 'derived' | 'assumed';

/** One metric's latest reading. A DIMENSIONED metric carries `by_dim` and has
 *  no single cell-level `value` — summing or picking one would be an
 *  invention, so the card must render the breakdown instead. */
export interface Observation {
  metric: string;
  unit: string | null;
  value: number | null;
  state: ObsState;
  provenance: Provenance | null;
  n: number | null;
  lo?: number | null;
  hi?: number | null;
  /** Absent on a read-time ratio, which is computed rather than observed. */
  day?: string | null;
  why?: string;
  dim?: string;
  by_dim?: Record<string, {
    value: number | null; state: ObsState;
    provenance: Provenance | null; n: number | null; why?: string;
  }>;
}

/** A same-unit sum. `complete` is false whenever anything was left out, and
 *  `missing` then names it — an incomplete total cannot travel without its
 *  gaps. There is deliberately no cross-unit total anywhere. */
export interface Subtotal {
  unit: string;
  value: number | null;
  contributors: number;
  complete: boolean;
  missing: { metric: string; why: string }[];
}

/** Days the COLLECTOR ran, over days it should have. Estate-wide, not
 *  per-cell: it reads one heartbeat ledger. Never label it with a domain. */
export interface Coverage {
  metrics_ok?: number;
  metrics_declared?: number;
  days_expected: number;
  days_collected: number;
  missing_days: string[];
}

export interface DomainTree {
  cadence?: string;
  north_star?: string;
  component?: string[];
  influences?: string[];
  guardrails?: string[];
  /** Absent when the cell declares no tree at all. */
  unresolved?: string[];
}

export interface DomainCell {
  ok: boolean;
  error?: string;
  realm: string;
  domain: string;
  since_days: number;
  north_star: Observation | null;
  metrics: Observation[];
  subtotals: Subtotal[];
  coverage: Coverage;
  provenance_floor: Provenance | null;
  gaps: { metric: string; state: ObsState; why: string }[];
  tree: DomainTree;
}

export interface DomainSurface {
  id: string;
  realm: string;
  domain: string;
  owner: string | null;
  label: string;
  rollup: string | null;
  metrics: number;
}

export interface PendingGrant {
  connector: string;
  tool: string;
  tier: string;
  metrics: string[];
}

export interface DomainsCatalogue {
  /** False only when the feature is off. A failed fetch is neither. */
  enabled?: boolean;
  axes: {
    realm?: { order?: string[]; labels?: Record<string, string> };
    domain?: { order?: string[]; labels?: Record<string, string> };
  };
  surfaces: DomainSurface[];
  cells: { realm: string; domain: string }[];
  problems: string[];
  pending_grants: PendingGrant[];
  coverage?: Coverage;
}

/** Sidebar readiness for one connected app — see `dashboard.apps_health`.
 *  FACTS plus a rolled-up `health` code; the owner-facing sentences are built
 *  in `lib/appHealth.tsx` (CLAUDE.md: the backend returns facts, the frontend
 *  writes the copy). */
export interface AppHealth {
  id: string;
  health: 'ready' | 'partial' | 'down' | 'off';
  enabled: boolean;
  /** null = the manifest declares no probe, which is NOT the same as "did not answer". */
  service: 'up' | 'down' | 'unknown' | null;
  auth_env: string | null;
  auth_set: boolean;
  tools_expected: number;
  tools_deployed: boolean;
  policy_expected: boolean;
  policy_present: boolean;
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

// A piece of agent/tool-produced media, resolved server-side to a same-origin,
// seekable URL (see ava_bridge/agent_media.py). Renders as a player/thumbnail
// on the assistant message and inside a tool-output card.
export interface MediaRef {
  url: string;
  kind: 'image' | 'video' | 'audio' | 'file';
  filename?: string;
  mime?: string;
}

export interface CotStep {
  kind: 'thinking' | 'text' | 'tool' | 'tool_result';
  text?: string;
  name?: string;
  // A tool call carries these once its result folds in (chatEvents.foldStep):
  id?: string;               // toolCallId — the fold key
  args?: unknown;            // what the tool was called with
  output?: string;           // the tool's result text (bounded)
  is_error?: boolean;        // the result was an error (snake, matching the wire)
  attachments?: MediaRef[];  // media the tool produced
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

// Does what Ava is CONFIGURED for match what the engine is actually serving?
// Mirrors ava_bridge/models.py TRUTHS; tests/test_model_state_vocabulary.py
// reconciles the two. Exactly one value is a disagreement.
export type BrainTruth =
  | 'agrees'        // the engine answered and holds the configured model
  | 'drifted'       // it answered and does NOT — every chat turn fails
  | 'mismatched'    // two CONFIG surfaces name different models. Turns still work
                    //   (the router rewrites the id) but a UI reading the wrong
                    //   one names a model that is not answering.
  | 'unreachable'   // it did not answer — silence is not evidence of a mismatch
  | 'unobservable'  // reachable but listed nothing, or a sandbox owns the endpoint
  | 'elsewhere'     // on another host, deliberately not probed from here
  | 'unconfigured'; // no brain is configured yet

export type ModelState =
  | 'resident'   // holding weights in memory right now
  | 'idle'       // engine up, has the model, not in memory (it loads on demand)
  | 'absent'     // engine up, does NOT have this model — never downloaded
  | 'offline'    // engine could not be reached
  | 'remote'     // runs somewhere else, so not in this box's memory
  | 'unknown';   // residency genuinely unobservable

/** WHOSE hardware a snapshot describes (ava_bridge/hwexporters.describe).
 *  `local` is the box the bridge runs on; `exporters` is another machine read
 *  through its node_exporter / GPU exporter. `error_code` carries the feature
 *  registry's regular codes: `remote_hardware_down` (configured, switched on,
 *  not answering — and nothing has been substituted for it, so the meters are
 *  blank) or, on a LOCAL reading, `remote_hardware_off` (a remote machine is
 *  configured but the switch is off). */
export interface HardwareSource {
  kind: 'local' | 'exporters';
  label: string;
  reachable: boolean;
  error_code: string;
  error: string;
}

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
    // Does the CONFIG agree with what the engine is actually serving? Closed
    // vocabulary (ava_bridge/models.py TRUTHS); "" for a row nobody compared.
    // Only `drifted` is a contradiction — the rest are ways of not knowing, and
    // a surface must never render them as a fault.
    drift?: BrainTruth | '';
    // What ava.yaml called this model. The row's `model` deliberately shows the
    // OBSERVED name when the two disagree (a name that is in memory nowhere
    // would be worse), so this is the other half of a drift message.
    config_label?: string;
    drift_detail?: { want: string; serving: string[]; matched: string };
    // Whether `state: 'resident'` was READ or concluded. False for an engine
    // that exposes no residency endpoint (vLLM, llama.cpp, MLX): it loads one
    // model at boot and holds it, so being served IS being resident — a correct
    // conclusion, but not an observation, and "In memory" reads identically
    // either way. Same distinction AllocModel.measured draws for resident_gib.
    state_measured?: boolean;
    // Why a row is not live, when the probe that found it out can say. Set for
    // the agent sandbox, where "Nothing is answering at its address" is the
    // wrong sentence — there is no address, there is a container, and the
    // reasons differ (stopped, unreachable, token mismatch).
    state_detail?: string;
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
  // Absent from a bridge older than this field; the monitor then reads as
  // "this machine", which is what such a bridge measures.
  machine?: HardwareSource;
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
  attachments?: MediaRef[];
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
  attachments?: MediaRef[]; // agent/tool-produced media, durable across reload
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
