import { describe, expect, it } from 'vitest';
import { applyOrder, moveTo } from './appOrder';

// The rule this pins is the one the BACKEND also implements
// (`connectors.apps()`): placed apps first, in their saved order; unplaced
// apps after them, keeping the server's order. Two implementations of one rule
// is the arrangement — the server so a reload is already right, the client so a
// drag lands without a round trip — so a change to one must fail here.

const apps = (...ids: string[]) => ids.map((id) => ({ id }));

describe('applyOrder', () => {
  it('leaves the list alone when nothing has been arranged', () => {
    const list = apps('a', 'b', 'c');
    expect(applyOrder(list, [])).toBe(list);   // same reference: no work done
  });

  it('sorts by the saved arrangement', () => {
    expect(applyOrder(apps('a', 'b', 'c'), ['c', 'a', 'b']).map((x) => x.id))
      .toEqual(['c', 'a', 'b']);
  });

  it('appends apps that have never been placed, in server order', () => {
    // `new` was connected after the owner last dragged anything. It must land
    // at the end, not in the middle of an arrangement it was never part of.
    expect(applyOrder(apps('a', 'new', 'b'), ['b', 'a']).map((x) => x.id))
      .toEqual(['b', 'a', 'new']);
  });

  it('keeps several unplaced apps in the order the server sent them', () => {
    expect(applyOrder(apps('x', 'y', 'a'), ['a']).map((x) => x.id))
      .toEqual(['a', 'x', 'y']);
  });

  it('ignores ids in the arrangement that no longer exist', () => {
    // An app removed in another tab is still named in the saved order.
    expect(applyOrder(apps('a', 'b'), ['gone', 'b', 'a']).map((x) => x.id))
      .toEqual(['b', 'a']);
  });

  it('does not mutate its input', () => {
    const list = apps('a', 'b', 'c');
    applyOrder(list, ['c', 'b', 'a']);
    expect(list.map((x) => x.id)).toEqual(['a', 'b', 'c']);
  });
});

describe('moveTo', () => {
  it('moves an item down', () => {
    expect(moveTo(['a', 'b', 'c'], 0, 2)).toEqual(['b', 'c', 'a']);
  });

  it('moves an item up', () => {
    expect(moveTo(['a', 'b', 'c'], 2, 0)).toEqual(['c', 'a', 'b']);
  });

  it('is a no-op when the item does not move', () => {
    const ids = ['a', 'b', 'c'];
    expect(moveTo(ids, 1, 1)).toBe(ids);
  });

  it('refuses an out-of-range index rather than dropping the item', () => {
    // A drop outside the list, or an Alt+Up on the first row.
    expect(moveTo(['a', 'b'], -1, 0)).toEqual(['a', 'b']);
    expect(moveTo(['a', 'b'], 0, 5)).toEqual(['a', 'b']);
    expect(moveTo(['a', 'b'], 5, 0)).toEqual(['a', 'b']);
  });

  it('does not mutate its input', () => {
    const ids = ['a', 'b', 'c'];
    moveTo(ids, 0, 2);
    expect(ids).toEqual(['a', 'b', 'c']);
  });
});
