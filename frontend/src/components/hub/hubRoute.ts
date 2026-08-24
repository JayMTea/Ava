// Pure address arithmetic for the Setup page: `#hub`, `#hub/<tab>` and — since
// Agent is the one tab with sections of its own — `#hub/agent/<sub>`.
//
// This file exists for the same reason provisionView.ts does. The SPA has no
// component-render harness, so anything that lives inside a component is
// verifiable only through the headless-Chromium tier. A router that silently
// resolves the wrong tab is exactly the kind of bug that tier is too coarse to
// catch, so the decision moves here and vitest covers every case.
//
// It also keeps the hash to ONE owner. App.tsx reads segment 0, HubView reads
// the rest through this module, and nothing else listens. Two hashchange
// listeners racing over one URL — with a legacy rewrite in the middle — is the
// bug class the App/HubView split was built to avoid; a third segment is not a
// reason to reintroduce it.
import type { TabId } from './shared';

export const AGENT_SUBTABS = [
  { id: 'runtime', label: 'Runtime' },
  { id: 'brain', label: 'Brain' },
  { id: 'persona', label: 'Persona' },
  { id: 'skills', label: 'Skills' },
  { id: 'memory', label: 'Memory' },
  { id: 'voice', label: 'Voice' },
  // Relocated from the agent gateway's own Control UI rather than invented
  // here. Both are CONFIGURATION — they outlive any one session — so they
  // belong beside Brain and Skills, not in the Agent console. `providers` sits
  // next to Brain because quota and spend are a recurring operator check, not a
  // one-time model pick; `secrets` next to Skills because skills are what
  // authenticate with them.
  { id: 'providers', label: 'Providers' },
  // `secrets` was RETIRED, not moved. It called `secrets.store.list` and
  // `secrets.store.set`, neither of which exists: the gateway's whole secrets
  // namespace is `secrets.reload` and `secrets.resolve` — reload the store
  // from disk, and resolve a reference for a caller. There is no enumerate and
  // no write, so nothing could have listed or added a secret. Ava's own
  // connector credentials were never here anyway (Setup → Connectors,
  // `secrets/env/<NAME>`), so nothing an owner can actually manage was lost.
  // An old `#hub/agent/secrets` degrades to the default sub and the address is
  // rewritten, so the URL bar stops advertising a tab that is gone.
] as const;

export type AgentSubTab = (typeof AGENT_SUBTABS)[number]['id'];

/**
 * Runtime is the default on purpose, not by alphabet.
 *
 * `#hub/agent` already means "go apply your changes" to PendingChangesBar, and
 * the drift board is what answers "is my agent actually live". Making anything
 * else the landing would silently change what every existing bare `#hub/agent`
 * link does.
 */
export const DEFAULT_SUB: AgentSubTab = 'runtime';

const SUB_IDS: readonly string[] = AGENT_SUBTABS.map((s) => s.id);

/**
 * Setup tabs that used to be top-level and now live inside Agent.
 *
 * Without this map an old bookmark, release note or doc link resolves through
 * the unknown-segment fallback and lands on Overview — which is
 * indistinguishable from "that feature was removed". The address is REWRITTEN
 * rather than merely resolved, so the URL bar tells the truth about where you
 * ended up and the next copy-paste carries the new address.
 */
export const MOVED_TABS: Record<string, AgentSubTab> = {
  persona: 'persona',
  voice: 'voice',
  memory: 'memory',
};

/** Retired tabs whose content merged into another top-level Setup tab. */
export const MERGED_TABS: Record<string, TabId> = {
  budgets: 'hardware',
};

/**
 * Retired tabs that moved to a different VIEW entirely. Resolving these is not
 * HubView's job — it can only report them, because changing segment 0 belongs
 * to App.tsx's router. Callers assign `location.hash` and let it pick them up.
 */
export const RELOCATED_TABS: Record<string, string> = {
  history: 'data/history',
};

export interface HubRoute {
  tab: TabId;
  sub: AgentSubTab;
  /** What the hash SHOULD read for this route, without the leading '#'. */
  canonical: string;
  /** Set when the address belongs to another view; the caller navigates there. */
  leaveTo?: string;
  /**
   * The address is not Setup's at all (`#vitals`, `#data/history`, an app id).
   *
   * HubView stays mounted for the instant between the hash changing and
   * App.tsx swapping the view, and its listener fires on that same event. Without
   * this flag it would "canonicalise" `#data` to `#hub` and yank the user back —
   * so `canonical` must never be acted on when this is set.
   */
  foreign?: boolean;
}

/**
 * Overview and the default sub-tab keep their URLs bare — the same rule `#hub`
 * already followed, extended one level down. A redundant segment in the bar is
 * a segment someone will later "fix" by hand and get wrong.
 */
export function hubHash(tab: TabId, sub: AgentSubTab = DEFAULT_SUB): string {
  if (tab === 'overview') return 'hub';
  if (tab === 'agent') return sub === DEFAULT_SUB ? 'hub/agent' : `hub/agent/${sub}`;
  return `hub/${tab}`;
}

/**
 * `hub` | `hub/<tab>` | `hub/agent/<sub>` → a route.
 *
 * Segment 2 is meaningful only under `agent`; anywhere else it is ignored
 * rather than 404'd, because a stale third segment on a valid tab should still
 * show you that tab.
 */
export function parseHubHash(hash: string, tabIds: readonly string[]): HubRoute {
  const parts = hash.replace(/^#\/?/, '').split('/');
  if (parts[0] !== 'hub') {
    return { tab: 'overview', sub: DEFAULT_SUB, canonical: 'hub', foreign: true };
  }

  const seg = parts[1] ?? '';

  const gone = RELOCATED_TABS[seg];
  if (gone) {
    return { tab: 'overview', sub: DEFAULT_SUB, canonical: 'hub', leaveTo: gone };
  }

  const moved = MOVED_TABS[seg];
  if (moved) {
    return { tab: 'agent', sub: moved, canonical: hubHash('agent', moved) };
  }

  const merged = MERGED_TABS[seg];
  if (merged) {
    return { tab: merged, sub: DEFAULT_SUB, canonical: hubHash(merged) };
  }

  const tab = (tabIds.includes(seg) ? seg : 'overview') as TabId;
  if (tab !== 'agent') {
    return { tab, sub: DEFAULT_SUB, canonical: hubHash(tab) };
  }

  const sub = (SUB_IDS.includes(parts[2] ?? '') ? parts[2] : DEFAULT_SUB) as AgentSubTab;
  return { tab: 'agent', sub, canonical: hubHash('agent', sub) };
}
