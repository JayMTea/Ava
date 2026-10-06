/** Grouping the rail's apps by the realm each one belongs to.
 *
 * WHY THE JOIN LIVES HERE AND NOT ON `AppEntry`. `AppEntry` is the wire shape of
 * /api/apps, and /api/apps does not send a realm — deliberately. The module that
 * knows the taxonomy already imports the connector registry, so teaching the
 * registry about the taxonomy would be a cycle, and it would put a YAML parse
 * behind the route that paints the sidebar on every boot. The realm is joined in
 * the browser instead, on `surface.owner === app.id`, exactly as `AppHealth` is
 * already joined to apps by id in the same component.
 *
 * NO VOCABULARY IS DEFINED HERE. Every realm value and label arrives in the
 * payload's `axes`. This module knows only how to order and label whatever it is
 * given, so a fork with a completely different taxonomy needs no edit.
 */
import type { AppEntry, DomainsCatalogue } from './types';

export interface RealmAxis {
  order: string[];
  labels: Record<string, string>;
}

export interface RealmGroup<T> {
  /** '' is the unrealmed run. It never gets a header. */
  realm: string;
  label: string;
  items: T[];
}

export interface RailRealms {
  /** True only when the payload said so. Loading and a failed fetch are both
   *  false — but only `enabled === false` may hide a destination, because
   *  hiding the entry on an error also hides the door to the page that would
   *  have explained the error. */
  enabled: boolean;
  axis: RealmAxis;
  /** app id -> every realm it owns an included surface in, in axis order. */
  byApp: ReadonlyMap<string, string[]>;
}

export const NO_AXIS: RealmAxis = { order: [], labels: {} };

/** A stable module-level constant, so a default prop keeps reference identity
 *  across renders and the flag-off path allocates nothing. */
export const RAIL_REALMS_OFF: RailRealms = {
  enabled: false, axis: NO_AXIS, byApp: new Map(),
};

export function axisIndex(order: string[], v: string): number {
  const i = order.indexOf(v);
  return i === -1 ? Number.POSITIVE_INFINITY : i;
}

export function realmLabel(axis: RealmAxis, realm: string): string {
  return axis.labels[realm] || realm;
}

export function railRealms(cat: DomainsCatalogue | null | undefined): RailRealms {
  if (!cat || cat.enabled === false) return RAIL_REALMS_OFF;
  const order = cat.axes?.realm?.order ?? [];
  const axis: RealmAxis = { order, labels: cat.axes?.realm?.labels ?? {} };
  const byApp = new Map<string, string[]>();
  for (const s of cat.surfaces ?? []) {
    // An excluded surface is one the rollup skips, so no cell exists for its
    // cell key. Filing an app under a realm whose card cannot be opened would
    // promise a page that 404s.
    if (!s.owner || s.rollup === 'excluded' || !s.realm) continue;
    const seen = byApp.get(s.owner);
    if (!seen) byApp.set(s.owner, [s.realm]);
    else if (!seen.includes(s.realm)) seen.push(s.realm);
  }
  for (const realms of byApp.values()) {
    realms.sort((a, b) => axisIndex(order, a) - axisIndex(order, b));
  }
  // The `enabled === false` case returned above, so reaching here means on.
  return { enabled: true, axis, byApp };
}

/** The realm an app is FILED under: its first in axis order. '' when it owns
 *  no included surface, which renders with no header at all. */
export function realmForApp(r: RailRealms, id: string): string {
  return r.byApp.get(id)?.[0] ?? '';
}

/** ', also in X and Y' for an app spanning several realms; '' otherwise.
 *  The rail files each app once, so this is the only channel that tells a
 *  screen-reader user the app has another home. */
export function alsoInLabel(r: RailRealms, id: string): string {
  const all = r.byApp.get(id) ?? [];
  if (all.length < 2) return '';
  const rest = all.slice(1).map((x) => realmLabel(r.axis, x));
  const joined = rest.length === 1
    ? rest[0]
    : `${rest.slice(0, -1).join(', ')} and ${rest[rest.length - 1]}`;
  return `, also in ${joined}`;
}

/** Keyed on INDEX, never on the realm's text. Realm values are unbounded — a
 *  value containing a space, interpolated into `aria-labelledby` (an IDREF
 *  *list*), resolves as two dangling references and the group announces
 *  unlabelled.
 *
 *  A static prefix rather than React's `useId`, deliberately. `useId` allocates
 *  from a per-render counter, so ADDING one call renumbers every later id in
 *  the tree — which broke the byte-identical-when-off guarantee for reasons
 *  that had nothing to do with the rail. The sidebar is mounted exactly once,
 *  and these ids are referenced only from inside it, so a fixed prefix is
 *  unique where it needs to be and stable where it is asserted. */
export function groupId(i: number): string {
  return `nav-realm-${i}`;
}

/** Group by realm, ordered by (realm position, INPUT position).
 *
 * Returns the input array BY REFERENCE in a single unlabelled group whenever
 * fewer than two distinct realms are present — so the flag-off and
 * single-realm cases render exactly the pre-grouping tree, and the caller can
 * detect that case with a reference check.
 */
export function groupByRealm<T>(
  items: T[], realmOf: (item: T) => string, axis: RealmAxis,
): RealmGroup<T>[] {
  const present = new Set<string>();
  for (const it of items) {
    const r = realmOf(it);
    if (r) present.add(r);
  }
  // Two headers to group one app is noise, not structure.
  if (present.size < 2) return [{ realm: '', label: '', items }];

  const decorated = items.map((item, i) => ({ item, i, r: realmOf(item) }));
  decorated.sort((a, b) => {
    const ai = a.r ? axisIndex(axis.order, a.r) : Number.POSITIVE_INFINITY;
    const bi = b.r ? axisIndex(axis.order, b.r) : Number.POSITIVE_INFINITY;
    // NOT `ai - bi`: both sides can be Infinity for unknown or empty realms,
    // and Infinity - Infinity is NaN, which makes Array.sort silently scramble
    // the unrealmed tail instead of leaving it in input order.
    return (ai === bi ? 0 : ai < bi ? -1 : 1) || (a.i - b.i);
  });

  const out: RealmGroup<T>[] = [];
  for (const d of decorated) {
    const last = out[out.length - 1];
    if (last && last.realm === d.r) last.items.push(d.item);
    else out.push({ realm: d.r, label: d.r ? realmLabel(axis, d.r) : '', items: [d.item] });
  }
  return out;
}

/** The realm each app is filed under, for a caller that has both halves. */
export function realmOfApp(r: RailRealms) {
  return (a: Pick<AppEntry, 'id'>) => realmForApp(r, a.id);
}


/** Which navigation destinations are visible, given the feature state.
 *
 * A destination is hidden ONLY when its feature is explicitly off. Loading and a
 * failed catalogue fetch both leave it visible, because the page it leads to is
 * what carries the banner explaining the failure — hiding the entry hides the
 * door to the explanation.
 *
 * Pure and exported so the rule is testable: the entries themselves live inside
 * a flyout that only exists once opened, so no static render can reach them.
 */
export function navVisible<T extends { feature?: string }>(
  items: readonly T[], r: RailRealms,
): T[] {
  return items.filter((it) => !it.feature || r.enabled !== false);
}
