import { describe, expect, it } from 'vitest';
import { focusFromHash, hashForCell } from './domainsRoute';

describe('focusFromHash', () => {
  it('reads a cell address', () => {
    expect(focusFromHash('#domains/r1/d1')).toEqual({ realm: 'r1', domain: 'd1' });
  });

  it('is empty for the index, for a partial address and for another view', () => {
    for (const h of ['#domains', '#/domains', '#domains/', '#domains/r1', '#hub/x/y', '']) {
      expect(focusFromHash(h)).toEqual({ realm: null, domain: null });
    }
  });

  it('ignores extra segments rather than failing', () => {
    expect(focusFromHash('#domains/r1/d1/extra')).toEqual({ realm: 'r1', domain: 'd1' });
  });

  it('round-trips a value containing a space', () => {
    const h = hashForCell('two words', 'd 1');
    expect(focusFromHash(h)).toEqual({ realm: 'two words', domain: 'd 1' });
  });

  it('does not throw on a malformed escape', () => {
    // A mistyped bookmark must not be a crash.
    expect(() => focusFromHash('#domains/%/d1')).not.toThrow();
    expect(focusFromHash('#domains/%/d1').realm).toBe('%');
  });
});
