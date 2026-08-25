import type { AppEntry } from './types';

// The sidebar's hand-arranged app order.
//
// The SAME rule the backend applies (`connectors.apps()`): an app the owner has
// placed sorts by its position; an app they have never dragged sorts after all
// of them, in whatever order the server sent. Both ends implement it because
// both need it — the server so a reload is already correct, the client so a
// drag lands instantly instead of waiting on a round trip — and they must not
// disagree, so the rule is stated once in each place and pinned by a test.
//
// Appending the unplaced is deliberate. Dropping them would make an app vanish
// from the sidebar the moment it was connected, and interleaving them by
// manifest `ui.order` would drop a stranger into the middle of an arrangement
// it was never part of.

/** `apps` in the owner's order. `order` is a list of ids; anything not in it
 *  keeps its incoming relative position, after everything that is. */
export function applyOrder<T extends Pick<AppEntry, 'id'>>(apps: T[], order: string[]): T[] {
  if (!order.length) return apps;
  const at = new Map(order.map((id, i) => [id, i]));
  // A stable sort by (placed?, position) — `apps.map` carries the original
  // index so unplaced apps keep the server's order rather than the engine's.
  return apps
    .map((a, i) => ({ a, i, p: at.has(a.id) ? (at.get(a.id) as number) : Infinity }))
    .sort((x, y) => (x.p - y.p) || (x.i - y.i))
    .map((x) => x.a);
}

/** Move the item at `from` so it lands at `to`, returning a NEW list.
 *  Out-of-range indices return the list untouched — a drop outside the list is
 *  a no-op, not a crash. */
export function moveTo(ids: string[], from: number, to: number): string[] {
  if (from === to) return ids;
  if (from < 0 || from >= ids.length || to < 0 || to >= ids.length) return ids;
  const next = ids.slice();
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
}
