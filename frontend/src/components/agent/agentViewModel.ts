import type { Tone } from '../hub/ui/Tile';
import type { Session } from '../../lib/agentApi';

// Pure selectors and the session vocabulary — the `provisionView.ts` of this tab.
//
// WHY THIS FILE EXISTS AT ALL
// ---------------------------
// Two reasons, both learned elsewhere in this repo:
//
// 1. The SPA has no component-render harness. A decision inside a component is
//    verifiable only through headless Chromium, which is too coarse to catch a
//    grouping rule that silently drops a session. So the decision moves here and
//    vitest covers it.
// 2. `tests/test_hub_uniformity.py` asserts there are EXACTLY SIX `.tone-*`
//    setters in the stylesheet and no per-component tone→colour rules. Nothing
//    below is a colour; everything is a tone NAME. A new status maps onto an
//    existing tone here, or it does not ship.
//
// `tests/test_agent_vocabulary.py` pins that this is the only place these
// mappings live — the same guard `DRIFT_LABEL`/`DRIFT_TONE` already has.

export type SessionState = Session['state'];

/**
 * Status → tone. Six tones, no new colours.
 *
 * `running` is `accent` rather than `ok` deliberately: `ok` reads as "finished
 * well", and a running session has not finished at all. `needs-input` is `info`
 * for the same reason — it is not a failure, it is a question.
 */
export const SESSION_TONE: Record<SessionState, Tone> = {
  idle: 'muted',
  queued: 'warn',
  running: 'accent',
  'needs-input': 'info',
  failed: 'err',
  archived: 'muted',
};

/** Status → what a person calls it. */
export const SESSION_LABEL: Record<SessionState, string> = {
  idle: 'idle',
  queued: 'queued',
  running: 'working',
  'needs-input': 'needs you',
  failed: 'failed',
  archived: 'archived',
};

/** Session kind → an icon that exists in `lib/icons.tsx`. */
export const SESSION_ICON: Record<string, string> = {
  coding: 'code',
  group: 'chats',
  other: 'chats',
};

export function sessionIcon(kind: string): string {
  return SESSION_ICON[kind] || 'chats';
}

export interface SessionGroup {
  id: string;
  label: string;
  sessions: Session[];
}

// Upstream groups sessions as Coding / Groups / Other. Kept, because it is the
// vocabulary an owner reading OpenClaw's own docs will already have — inventing
// different names for the same three buckets helps nobody.
const GROUP_ORDER = ['coding', 'group', 'other'] as const;
const GROUP_LABEL: Record<string, string> = {
  coding: 'Coding', group: 'Groups', other: 'Other',
};

/**
 * Bucket sessions for the list.
 *
 * Rules that matter:
 *   * An EMPTY group is omitted rather than rendered as a header with nothing
 *     under it — a heading over a void reads as a loading failure.
 *   * A custom `group` name wins over `kind`, and sorts after the built-ins, so
 *     an owner's own grouping is never silently reordered into the middle.
 *   * Archived sessions are excluded here rather than filtered by the caller:
 *     every caller would have to remember, and one that forgot would show the
 *     owner a list that quietly grows forever.
 *   * Within a group, most-recent first. Unread does NOT float to the top —
 *     a list that reorders itself while you read it is a list you lose your
 *     place in.
 */
export function groupSessions(sessions: readonly Session[]): SessionGroup[] {
  const live = sessions.filter((s) => s.state !== 'archived');
  const buckets = new Map<string, Session[]>();
  for (const s of live) {
    const key = s.group || s.kind || 'other';
    const arr = buckets.get(key);
    if (arr) arr.push(s);
    else buckets.set(key, [s]);
  }
  const custom = [...buckets.keys()]
    .filter((k) => !(GROUP_ORDER as readonly string[]).includes(k))
    .sort();
  const order = [...GROUP_ORDER, ...custom];
  const out: SessionGroup[] = [];
  for (const key of order) {
    const rows = buckets.get(key);
    if (!rows || !rows.length) continue;
    out.push({
      id: key,
      label: GROUP_LABEL[key] || key,
      sessions: [...rows].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0)),
    });
  }
  return out;
}

// ---- chat-origin sessions --------------------------------------------------
//
// The owner's contract for this tab: Chats is the ONE place you talk; the
// Agent tab is where you watch and operate. The Chats tab keys its gateway
// sessions `<prefix>-<chat id>` (ava_bridge/chat_store.py `chat_session`), so
// those sessions surface HERE too — shown, labeled as what they are, rather
// than hidden or left masquerading as agent-native work.
//
// The prefix is env-configurable (AVA_OC_SESSION) and served as
// `session_prefix` on /api/gateway/status. Hardcoding 'ava-phone' anywhere but
// this default would break every deployment that set the variable.

/** What the prefix is when /api/gateway/status has not answered yet (or the
 *  bridge predates the field). Matches ava_bridge/config.py OC_SESSION. */
export const DEFAULT_SESSION_PREFIX = 'ava-phone';

const shortKey = (key: string): string => key.split(':').pop() || key;

/**
 * The chat id behind a session key, or null for an agent-native session.
 *
 * Accepts both the full key ('agent:main:ava-phone-c1') and the short form
 * ('ava-phone-c1'): sessions.list and the gateway's chat events both carry the
 * full form today, but nothing guarantees a future caller does, and the short
 * form is what the bridge itself mints.
 *
 * The separator dash is required — 'ava-phone2-c1' under prefix 'ava-phone' is
 * NOT a chat session, it is a different session that happens to share nine
 * characters.
 */
export function chatIdOfSession(key: string, prefix: string): string | null {
  if (!prefix) return null;
  const short = shortKey(key);
  if (!short.startsWith(`${prefix}-`)) return null;
  return short.slice(prefix.length + 1) || null;
}

/** The one chat the list needs to label a session — lib/types.ts ChatSummary
 *  satisfies this structurally. */
export interface ChatRef { id: string; title?: string }

/** A session as the console lists it: `Session` plus its Chats identity. */
export interface ConsoleSession extends Session {
  /** Set on chat-origin rows — the chat behind this session. */
  chatId?: string;
  /** 'deleted from Chats' when the chat no longer exists to link back to. */
  note?: string;
  /** Icon override: chat-origin rows wear the Chats glyph, not their kind's. */
  icon?: string;
}

export interface ConsoleGroup {
  id: string;
  label: string;
  sessions: ConsoleSession[];
}

export const CHATS_GROUP_ID = 'ava-chats';

/**
 * `groupSessions`, with the chat-origin sessions split into their own group.
 *
 * Rules on top of groupSessions' own:
 *   * Chat-origin sessions land under 'Your chats', labeled with the REAL chat
 *     title — the row must read as the conversation the owner had, not as the
 *     key the bridge minted for it. An untitled live chat reads 'New chat',
 *     the same fallback the sidebar uses, so the two surfaces agree.
 *   * A chat that was deleted from Chats still shows (its transcript is a
 *     server fact this console audits), titled by its cid with the note
 *     'deleted from Chats' — silently dropping it would hide agent history.
 *   * The BARE prefix session is 'Background': voice warm-ups and turns
 *     without a chat land there, and its raw name is the one key an owner is
 *     guaranteed to meet and least able to interpret.
 *   * Agent-native sessions keep their existing groups, untouched.
 */
export function groupConsoleSessions(
  sessions: readonly Session[],
  prefix: string,
  chats: readonly ChatRef[],
): ConsoleGroup[] {
  const titles = new Map(chats.map((c) => [c.id, c.title || '']));
  const ava: ConsoleSession[] = [];
  const native: Session[] = [];
  for (const s of sessions) {
    if (s.state === 'archived') continue; // the rule groupSessions applies
    if (shortKey(s.id) === prefix) {
      ava.push({ ...s, title: 'Background', icon: 'chats' });
      continue;
    }
    const cid = chatIdOfSession(s.id, prefix);
    if (cid == null) { native.push(s); continue; }
    const known = titles.has(cid);
    ava.push({
      ...s,
      chatId: cid,
      icon: 'chats',
      title: known ? (titles.get(cid) || 'New chat') : cid,
      note: known ? undefined : 'deleted from Chats',
    });
  }
  const out: ConsoleGroup[] = [];
  if (ava.length) {
    out.push({
      id: CHATS_GROUP_ID,
      label: 'Your chats',
      sessions: [...ava].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0)),
    });
  }
  return [...out, ...groupSessions(native)];
}

/** Total unread across the live sessions — the nav badge's number. */
export function unreadRollup(sessions: readonly Session[]): number {
  return sessions.reduce(
    (n, s) => n + (s.state === 'archived' ? 0 : Math.max(0, s.unread || 0)), 0);
}

/** How many sessions are actually doing something right now. */
export function busyCount(sessions: readonly Session[]): number {
  return sessions.filter((s) => s.state === 'running' || s.state === 'queued').length;
}

export interface PrChipView {
  label: string;
  tone: Tone;
  title: string;
}

export interface PrRef {
  number?: number;
  repo?: string;
  branch?: string;
  additions?: number;
  deletions?: number;
  ci?: 'passing' | 'failing' | 'pending' | string;
  state?: 'draft' | 'open' | 'merged' | 'closed' | string;
}

/**
 * A pull-request chip, built from Badge + text rather than a new primitive.
 *
 * CI state drives the tone because that is the thing you glance for; the PR's
 * own state is in the title, where you look when you have already glanced.
 * A PR with no CI is `muted`, not `ok` — "no result" and "passed" are different
 * facts and a green chip for the former is a lie.
 */
export function prChipView(pr: PrRef): PrChipView {
  const tone: Tone =
    pr.ci === 'passing' ? 'ok'
      : pr.ci === 'failing' ? 'err'
        : pr.ci === 'pending' ? 'warn'
          : 'muted';
  const num = pr.number ? `#${pr.number}` : 'PR';
  const diff = (pr.additions != null || pr.deletions != null)
    ? ` +${pr.additions ?? 0} −${pr.deletions ?? 0}`
    : '';
  return {
    label: `${num}${diff}`,
    tone,
    title: [pr.repo, pr.branch, pr.state, pr.ci && `CI ${pr.ci}`]
      .filter(Boolean).join(' · '),
  };
}

/** "3 unread" / "" — the aria label a bare dot cannot carry. */
export function unreadLabel(n: number): string {
  return n > 0 ? `${n} unread` : '';
}


// ---- activity --------------------------------------------------------------

export interface RunRow {
  id: string;
  sessionId?: string;
  at?: number;
  status?: string;
  title?: string;
}

export interface DayGroup {
  key: string;
  label: string;
  runs: RunRow[];
}

/** Run status → tone. Runs are past tense, so `ok` is honest here. */
export const RUN_TONE: Record<string, Tone> = {
  done: 'ok',
  ok: 'ok',
  failed: 'err',
  error: 'err',
  degraded: 'warn',
  cancelled: 'muted',
  running: 'accent',
};

export function runTone(status?: string): Tone {
  return RUN_TONE[String(status || '').toLowerCase()] || 'muted';
}

/**
 * Group runs into days, newest first.
 *
 * "Today" and "Yesterday" are computed against the VIEWER's clock rather than
 * stored, because a page left open overnight would otherwise keep calling
 * yesterday "Today". `now` is a parameter for exactly that reason — it makes
 * the boundary testable instead of a thing you find out about at midnight.
 *
 * A run with no timestamp is not dropped: it goes in an "Undated" bucket at the
 * end. Silently hiding a row because one field is missing is how an audit view
 * quietly stops being one.
 */
export function groupRunsByDay(runs: readonly RunRow[], now = Date.now()): DayGroup[] {
  const dayOf = (ms: number) => {
    const d = new Date(ms);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  };
  const today = dayOf(now);
  const yesterday = dayOf(now - 86_400_000);

  const buckets = new Map<string, RunRow[]>();
  const undated: RunRow[] = [];
  for (const r of runs) {
    if (!r.at) { undated.push(r); continue; }
    const k = dayOf(r.at);
    const arr = buckets.get(k);
    if (arr) arr.push(r);
    else buckets.set(k, [r]);
  }

  const out: DayGroup[] = [...buckets.keys()]
    .sort((a, b) => b.localeCompare(a))
    .map((k) => ({
      key: k,
      label: k === today ? 'Today' : k === yesterday ? 'Yesterday' : k,
      runs: (buckets.get(k) || []).sort((a, b) => (b.at || 0) - (a.at || 0)),
    }));
  if (undated.length) out.push({ key: 'undated', label: 'Undated', runs: undated });
  return out;
}

// ---- decision receipts -----------------------------------------------------

export interface Receipt {
  id?: string;
  kind?: string;
  decision?: string;
  enforced?: boolean;
  detail?: string;
  provenance?: string;
}

/**
 * "Enforced" vs "Attribution only" — and the distinction is the whole point.
 *
 * An enforced receipt means the decision actually gated something. An
 * attribution-only one means it was recorded but did not stop anything. Showing
 * them identically would let a reader conclude a policy was applied when it was
 * merely noted, which is the single most misleading thing an audit view can do.
 */
export function receiptBadge(r: Receipt): { label: string; tone: Tone } {
  if (r.enforced) return { label: 'enforced', tone: 'accent' };
  return { label: 'attribution only', tone: 'muted' };
}
