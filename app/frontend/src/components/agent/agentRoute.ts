// Pure address arithmetic for the Agent console: `#agent`, a session, a side
// panel, a run, a job.
//
// Same reasoning as components/hub/hubRoute.ts, which this deliberately mirrors:
// the SPA has no component-render harness, so a router living inside a component
// is verifiable only through headless Chromium — too coarse to catch an address
// that silently resolves to the wrong thing. The decision moves here and vitest
// covers every form.
//
// Deliberately its own parser rather than a generalised one. Agent needs up to
// four segments, two of which are OPAQUE SERVER IDS; the shared hash-tab helper
// this used to contrast with handled the simple case (one segment, no legacy
// map) and went with the Data page. Widening a shared helper to cover Agent
// would have made every other view pay for Agent's complexity.

export const AGENT_SECTIONS = [
  { id: 'sessions', label: 'Sessions' },
  { id: 'activity', label: 'Activity' },
  { id: 'automations', label: 'Automations' },
] as const;

export type AgentSection = (typeof AGENT_SECTIONS)[number]['id'];

// 'side' (a read-only side chat) is retired, deliberately NOT listed: as
// read-only it duplicated the thread it sat beside, and the one place you talk
// is Chats. A legacy '#agent/s/x/side' address needs no special case —
// parseAgentHash treats an unknown panel as no panel, so it canonicalises to
// the session instead of dying. The test suite pins that.
// `browser` was retired: it embedded /apps/openclaw/browser, and there is no
// `openclaw` connector to serve that path — OpenClaw is deliberately not an
// app — so the iframe 404'd on every install. parseAgentHash treats an
// unknown panel as no panel, so an old bookmark degrades to the thread
// rather than breaking.
export const SIDE_PANELS = [
  'terminal', 'files', 'tasks', 'review',
] as const;

export type SidePanel = (typeof SIDE_PANELS)[number];

export const DEFAULT_SECTION: AgentSection = 'sessions';

const SECTION_IDS: readonly string[] = AGENT_SECTIONS.map((s) => s.id);
const PANEL_IDS: readonly string[] = SIDE_PANELS;

export interface AgentRoute {
  section: AgentSection;
  sessionId: string | null;
  panel: SidePanel | null;
  runId: string | null;
  jobId: string | null;
  /** Without the leading `#`. Never act on this when `foreign` is set. */
  canonical: string;
  /** The address is not Agent's — the view is kept alive and must not react. */
  foreign?: boolean;
}

/**
 * Build an address.
 *
 * The default section keeps its URL bare, the same rule `#hub` follows. A
 * redundant segment in the bar is one somebody will later "fix" by hand and get
 * wrong.
 */
export function agentHash(r: Partial<AgentRoute>): string {
  const section = r.section ?? DEFAULT_SECTION;
  if (section === 'sessions') {
    if (!r.sessionId) return 'agent';
    return r.panel
      ? `agent/s/${r.sessionId}/${r.panel}`
      : `agent/s/${r.sessionId}`;
  }
  if (section === 'activity') {
    return r.runId ? `agent/activity/run/${r.runId}` : 'agent/activity';
  }
  return r.jobId ? `agent/automations/${r.jobId}` : 'agent/automations';
}

const EMPTY: Omit<AgentRoute, 'canonical'> = {
  section: DEFAULT_SECTION, sessionId: null, panel: null, runId: null,
  jobId: null,
};

/**
 * Parse an address.
 *
 * WHY THE `s/` AND `run/` VERBS. A bare id in segment 1 is indistinguishable
 * from a section name: a session genuinely called `activity` becomes
 * unreachable, and — worse — a MISTYPED section name resolves as a session id
 * and renders "no such session" instead of falling back. The verb makes segment
 * 1 a closed vocabulary and segment 2 a free one. `hubRoute` never faced this
 * because every Setup segment is closed.
 *
 * IDS ARE NOT VALIDATED. The list to validate against is asynchronous, and a
 * missing session renders "this session no longer exists" with a way back
 * rather than redirecting. A redirect erases the evidence and reads as a bug.
 *
 * Canonicalisation DROPS what it cannot resolve and never invents: junk after a
 * session id falls back to the session, an empty id falls back to the section.
 */
export function parseAgentHash(hash: string): AgentRoute {
  const parts = hash.replace(/^#\/?/, '').split('/').filter((p) => p !== '');
  if (parts[0] !== 'agent') {
    return { ...EMPTY, canonical: 'agent', foreign: true };
  }

  const seg = parts[1] ?? '';

  if (seg === 's') {
    const sessionId = parts[2] ?? '';
    if (!sessionId) return { ...EMPTY, canonical: 'agent' };
    const raw = parts[3] ?? '';
    const panel = (PANEL_IDS.includes(raw) ? raw : null) as SidePanel | null;
    return {
      ...EMPTY, sessionId, panel,
      canonical: agentHash({ section: 'sessions', sessionId, panel }),
    };
  }

  if (seg === 'activity') {
    const runId = parts[2] === 'run' ? (parts[3] ?? '') : '';
    return {
      ...EMPTY, section: 'activity', runId: runId || null,
      canonical: agentHash({ section: 'activity', runId: runId || null }),
    };
  }

  if (seg === 'automations') {
    const jobId = parts[2] ?? '';
    return {
      ...EMPTY, section: 'automations', jobId: jobId || null,
      canonical: agentHash({ section: 'automations', jobId: jobId || null }),
    };
  }

  // An unknown segment is the console, not a 404. Same rule Setup follows: a
  // stale address should still land you somewhere useful.
  return { ...EMPTY, canonical: 'agent' };
}

/** Is this section id one we render? Used by the guard test, not by the view. */
export function isSection(id: string): id is AgentSection {
  return SECTION_IDS.includes(id);
}
