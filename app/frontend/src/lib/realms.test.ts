/** Invented vocabulary only — tracked source may not carry the owner's. */
import { describe, expect, it } from 'vitest';
import {
  alsoInLabel, axisIndex, groupByRealm, groupId, NO_AXIS, navVisible, 
  RAIL_REALMS_OFF, railRealms,realmForApp, realmLabel,
} from './realms';
import type { DomainsCatalogue } from './types';

const AXIS = { order: ['r1', 'r2', 'r3'], labels: { r1: 'One', r2: 'Two' } };
const app = (id: string) => ({ id });

function cat(surfaces: Partial<DomainsCatalogue['surfaces'][0]>[]): DomainsCatalogue {
  return {
    enabled: true,
    axes: { realm: { order: AXIS.order, labels: AXIS.labels } },
    surfaces: surfaces.map((s) => ({
      id: 'x/y', realm: 'r1', domain: 'd1', owner: 'app-a', label: 'x',
      rollup: null, metrics: 1, ...s,
    })) as DomainsCatalogue['surfaces'],
    cells: [], problems: [], pending_grants: [],
  };
}

describe('groupByRealm', () => {
  it('returns the input BY REFERENCE when fewer than two realms are present', () => {
    // The flag-off guarantee depends on this: no copy, no re-sort, no wrapper.
    const apps = [app('a'), app('b')];
    const groups = groupByRealm(apps, () => '', NO_AXIS);
    expect(groups).toHaveLength(1);
    expect(groups[0].realm).toBe('');
    expect(groups[0].label).toBe('');
    expect(groups[0].items).toBe(apps);
  });

  it('does not group when only one realm is present', () => {
    const apps = [app('a'), app('b')];
    const groups = groupByRealm(apps, () => 'r1', AXIS);
    expect(groups).toHaveLength(1);
    expect(groups[0].items).toBe(apps);
  });

  it('orders groups by axis position and rows by input position', () => {
    const apps = [app('a'), app('b'), app('c')];
    const of = (x: { id: string }) => ({ a: 'r2', b: 'r1', c: 'r2' }[x.id] as string);
    const groups = groupByRealm(apps, of, AXIS);
    expect(groups.map((g) => g.realm)).toEqual(['r1', 'r2']);
    expect(groups[1].items.map((i) => i.id)).toEqual(['a', 'c']);
  });

  it('keeps unknown realms in input order rather than scrambling them', () => {
    // The Infinity - Infinity => NaN comparator trap: a NaN comparator makes
    // Array.sort unstable, which silently reorders the unrealmed tail.
    const apps = [app('a'), app('b'), app('c')];
    const of = (x: { id: string }) => ({ a: 'zzz', b: 'r1', c: 'yyy' }[x.id] as string);
    const groups = groupByRealm(apps, of, AXIS);
    expect(groups[0].realm).toBe('r1');
    expect(groups.slice(1).flatMap((g) => g.items.map((i) => i.id))).toEqual(['a', 'c']);
  });

  it('puts the unrealmed run last and never gives it a header', () => {
    const apps = [app('a'), app('b'), app('c')];
    const of = (x: { id: string }) => ({ a: '', b: 'r1', c: 'r2' }[x.id] as string);
    const groups = groupByRealm(apps, of, AXIS);
    expect(groups[groups.length - 1].realm).toBe('');
    expect(groups[groups.length - 1].label).toBe('');
    expect(groups.filter((g) => g.label).length).toBe(2);
  });

  it('preserves every item exactly once, in a stable order', () => {
    const apps = [app('a'), app('b'), app('c'), app('d')];
    const of = (x: { id: string }) => ({ a: 'r2', b: '', c: 'r1', d: 'r2' }[x.id] as string);
    const flat = groupByRealm(apps, of, AXIS).flatMap((g) => g.items.map((i) => i.id));
    expect(flat.sort()).toEqual(['a', 'b', 'c', 'd']);
    expect(apps.map((a) => a.id)).toEqual(['a', 'b', 'c', 'd']); // input unmutated
  });
});

describe('railRealms', () => {
  it('is off for a null payload and for an explicitly disabled one', () => {
    expect(railRealms(null)).toBe(RAIL_REALMS_OFF);
    expect(railRealms({ ...cat([]), enabled: false })).toBe(RAIL_REALMS_OFF);
    expect(railRealms(null).byApp.size).toBe(0);
  });

  it('never files an app under an excluded surface', () => {
    // An excluded surface has no cell, so a header for it would link nowhere.
    const r = railRealms(cat([{ realm: 'r2', rollup: 'excluded' }]));
    expect(realmForApp(r, 'app-a')).toBe('');
  });

  it('files a multi-realm app under its first realm in axis order', () => {
    const r = railRealms(cat([
      { id: 'x/2', realm: 'r2' }, { id: 'x/1', realm: 'r1' },
    ]));
    expect(realmForApp(r, 'app-a')).toBe('r1');
    expect(r.byApp.get('app-a')).toEqual(['r1', 'r2']);
  });
});

describe('labels', () => {
  it('falls back to the raw realm id when the axis declares no label', () => {
    expect(realmLabel(AXIS, 'r3')).toBe('r3');
    expect(realmLabel(AXIS, 'r1')).toBe('One');
  });

  it('names the other realms of a multi-realm app, and nothing for one', () => {
    const one = railRealms(cat([{ realm: 'r1' }]));
    expect(alsoInLabel(one, 'app-a')).toBe('');
    expect(alsoInLabel(one, 'nobody')).toBe('');
    const two = railRealms(cat([{ id: 'x/1', realm: 'r1' }, { id: 'x/2', realm: 'r2' }]));
    expect(alsoInLabel(two, 'app-a')).toBe(', also in Two');
  });

  it('builds a group id with no space, whatever the realm value is', () => {
    // aria-labelledby is an IDREF LIST: a space would split it into two
    // dangling references and the group would announce unlabelled.
    expect(groupId(2)).not.toContain(' ');
  });

  it('sorts unknown realms after known ones', () => {
    expect(axisIndex(AXIS.order, 'r1')).toBe(0);
    expect(axisIndex(AXIS.order, 'nope')).toBe(Number.POSITIVE_INFINITY);
  });
});

describe('navVisible', () => {
  const items = [{ id: 'a' }, { id: 'b', feature: 'domains' }];

  it('hides a feature entry only when the payload says the feature is off', () => {
    expect(navVisible(items, RAIL_REALMS_OFF).map((i) => i.id)).toEqual(['a']);
  });

  it('keeps it visible while loading and after a failed fetch', () => {
    // Hiding the entry on an error hides the door to the page that would have
    // explained the error.
    const on = { enabled: true, axis: NO_AXIS, byApp: new Map() };
    expect(navVisible(items, on).map((i) => i.id)).toEqual(['a', 'b']);
  });

  it('never touches an entry that declares no feature', () => {
    expect(navVisible([{ id: 'a' } as { id: string; feature?: string }],
                      RAIL_REALMS_OFF)).toHaveLength(1);
  });
});
