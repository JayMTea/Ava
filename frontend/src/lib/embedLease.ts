/**
 * The lease behind an app frame: what the bridge handed over, when, and for how long.
 *
 * With `apps.origin` configured the frame loads from a second origin on a token the
 * bridge mints for a few minutes and then swaps for a cookie that lives for hours and
 * slides forward on use (ava_bridge/apps_origin.py, "two lifetimes"). Neither survives
 * a phone that has been asleep for long enough, and a frozen page cannot renew anything
 * — so the shell has to know when a lease is past saving and mint again on resume.
 * These are the decisions, kept pure so they can be tested without a browser; AppFrame
 * owns the timers, the fetches and the DOM.
 */
export interface EmbedLease {
  readonly src: string;
  /** The origin the frame lives on: the apps origin, or Ava's own when unconfigured. */
  readonly origin: string;
  readonly mintedAt: number;
  readonly tokenTtlMs: number;
  readonly cookieTtlMs: number;
}

/** What `GET /api/apps/<id>/embed` answers. The lifetimes are newer than the URL. */
export interface EmbedGrant {
  url: string;
  isolated?: boolean;
  token_ttl_s?: number;
  cookie_ttl_s?: number;
}

/** The bridge's numbers before it learned to state them, so an older bridge still
 *  gets a shell that re-mints — on the schedule that bridge actually enforces. */
export const DEFAULT_TOKEN_TTL_S = 300;
export const DEFAULT_COOKIE_TTL_S = 12 * 60 * 60;

function seconds(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : fallback;
}

export function leaseFrom(grant: EmbedGrant, src: string, origin: string, now: number): EmbedLease {
  return {
    src,
    origin,
    mintedAt: now,
    tokenTtlMs: seconds(grant.token_ttl_s, DEFAULT_TOKEN_TTL_S) * 1000,
    cookieTtlMs: seconds(grant.cookie_ttl_s, DEFAULT_COOKIE_TTL_S) * 1000,
  };
}

/** Re-mint a little before the bridge would refuse, so a resume never races the clock. */
const MARGIN = 0.1;

/**
 * Is this lease past saving? Before the frame's first `load` only the URL token counts —
 * it was never exchanged. After it, the cookie does. Renewals the bridge did while the
 * page was awake are invisible from here, so this errs towards minting again: the cost
 * is one reload of a frame that was about to die anyway, and only ever on resume.
 */
export function leaseExpired(lease: EmbedLease, loaded: boolean, now: number): boolean {
  const life = loaded ? lease.cookieTtlMs : lease.tokenTtlMs;
  return now - lease.mintedAt >= life * (1 - MARGIN);
}

/**
 * How often to touch the apps origin while the page is awake. A quarter of the cookie's
 * life lands every touch well inside the bridge's half-life renewal window, so an app
 * nobody clicks in all day stays reachable; never faster than a minute, whatever a
 * bridge says.
 */
export function keepaliveIntervalMs(lease: EmbedLease): number {
  return Math.max(60_000, Math.floor(lease.cookieTtlMs / 4));
}

export function keepaliveUrl(lease: EmbedLease, id: string): string {
  return `${lease.origin}/apps/${encodeURIComponent(id)}/.ava/keepalive`;
}

/**
 * The message a dead frame posts (ava_bridge/apps_origin.reconnect_page): which app,
 * and the path it was on. Null for anything else; the app root when the path is
 * missing or unusable. `appFrameDestination` re-validates whatever comes back.
 */
export function expiredFramePath(data: unknown, id: string): string | null {
  if (!data || typeof data !== 'object') return null;
  const m = data as { type?: unknown; cid?: unknown; path?: unknown };
  if (m.type !== 'ava:embed-expired' || m.cid !== id) return null;
  return typeof m.path === 'string' && m.path.startsWith('/') ? m.path : '/';
}

/** At most one re-mint per this long on a frame's say-so, so a frame that dies on
 *  arrival (a clock a day off, a bridge minting under another secret) cannot spin. */
export const REMINT_COOLDOWN_MS = 10_000;
