// One connector's agent surface, as `GET /api/apps/{cid}/actions` reports it
// (ava_bridge/connectors.py `app_actions`).
//
// The wording for `transport` lives HERE rather than in the panel that first
// needed it, because two surfaces now show it — Setup → Connectors and the
// ActionConsole — and the previous single copy was a private const. A protocol
// name the owner reads in two places must not be able to disagree with itself;
// that is the same class of bug as the badge this table replaced, which called
// every connector with tools "MCP".

/** How a connector's tools reach Ava. Rendered verbatim from the backend's
 *  `transport` field — the UI must never re-derive it. */
export const TRANSPORT_LABEL: Record<string, string> = {
  mcp: 'MCP',
  discover: 'tool facade',
  rest: 'REST',
  none: '',
};

export const TRANSPORT_HINT: Record<string, string> = {
  mcp: 'A real Model Context Protocol server — Ava speaks MCP to it.',
  discover: "Ava's own ava-tools/1 HTTP facade — MCP-shaped, but not MCP.",
  rest: "Statically declared actions proxied to the app's REST API.",
  none: 'No agent surface — UI-only, or a push-only device.',
};

/** Where the tool list came from. An empty list means something different in
 *  each case, which is the whole reason the backend reports it. */
export type SurfaceSource = 'live' | 'cache' | 'declared' | 'none';

export type Tool = {
  name: string;
  description?: string;
  /** The tier Ava will ENFORCE — `action_access`, not the app's own claim. */
  access?: string;
  /** True when Ava asks the owner before running it (`needs_confirm`). */
  confirm?: boolean;
};

export type Surface = {
  tools: Tool[];
  transport: string;
  source: SurfaceSource;
  error: string | null;
};

/** What a tier MEANS at the moment of a call. A permissions surface answers
 *  "will this just happen?"; naming the taxonomy back at the owner does not. */
export const TIER_HINT: Record<string, string> = {
  read: 'Reads only — runs without asking',
  sensitive: 'Discloses something — asks the first time',
  write: 'Changes something — asks the first time',
  destructive: 'Destructive — asks every time, and cannot be granted away',
  physical: 'Acts in the real world — asks every time, and cannot be granted away',
};

const SOURCES: SurfaceSource[] = ['live', 'cache', 'declared', 'none'];

/** Normalise one `/actions` response. Tolerant on purpose: this panel is the
 *  ONLY view of an app that ships no UI, so a field the backend stops sending
 *  must degrade to a plainer console, never to a crash. */
export function parseSurface(raw: unknown): Surface {
  const r = (raw ?? {}) as Record<string, unknown>;
  const rawTools = Array.isArray(r.tools) ? r.tools : [];
  const tools: Tool[] = rawTools
    .filter((t): t is Record<string, unknown> => !!t && typeof t === 'object')
    .map((t) => ({
      name: String(t.name ?? ''),
      description: typeof t.description === 'string' ? t.description : '',
      access: typeof t.access === 'string' ? t.access : undefined,
      confirm: t.confirm === true,
    }))
    .filter((t) => t.name !== '');
  const source = SOURCES.includes(r.source as SurfaceSource)
    ? (r.source as SurfaceSource)
    : 'none';
  return {
    tools,
    transport: typeof r.transport === 'string' ? r.transport : 'none',
    source,
    error: typeof r.error === 'string' && r.error ? r.error : null,
  };
}

export type ConsoleState =
  | { kind: 'loading' }
  /** The request itself failed — Ava's own API, not the app's. */
  | { kind: 'unavailable'; detail: string }
  /** There are tools to show. `stale` means the app did not answer and this is
   *  the last list it served. */
  | { kind: 'tools'; tools: Tool[]; stale: boolean; detail: string | null }
  /** Dynamic connector, nothing cached, and the app could not be asked. */
  | { kind: 'unreachable'; detail: string }
  /** The app answered and genuinely lists nothing. Not the same as the above. */
  | { kind: 'silent' }
  /** The manifest declares no agent surface at all. */
  | { kind: 'none' };

/**
 * The console's one decision, kept out of the component so it can be tested
 * without a DOM (this project's vitest runs in node — no jsdom).
 *
 * The distinction that matters, and the one the original console could not
 * make: an empty list is either "this app has no agent surface", which is a
 * fact about the manifest, or "I could not ask it", which is an outage. They
 * send the owner to completely different places, and collapsing them into
 * "This app declares no agent actions." is what made a healthy 25-tool MCP app
 * look like a misconfigured one.
 */
export function consoleState(
  surface: Surface | null,
  fetchError: string | null,
): ConsoleState {
  if (fetchError) return { kind: 'unavailable', detail: fetchError };
  if (!surface) return { kind: 'loading' };
  if (surface.tools.length > 0) {
    const stale = surface.source === 'cache';
    return { kind: 'tools', tools: surface.tools, stale, detail: stale ? surface.error : null };
  }
  // `transport: none` FIRST. The backend sets `error` in that case too ("this
  // connector declares no agent surface"), and reading it as an outage would
  // put a red herring in front of someone whose app is behaving exactly as
  // configured.
  if (surface.transport === 'none') return { kind: 'none' };
  if (surface.error) return { kind: 'unreachable', detail: surface.error };
  if (surface.source === 'live') return { kind: 'silent' };
  return { kind: 'none' };
}
