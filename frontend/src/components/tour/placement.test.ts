// Geometry for the walkthrough card. Pure, so it is testable — the SPA has no
// component-render harness (see components/hub/provisionView.test.ts, which
// exists for the same reason), and untestable geometry is geometry that is
// wrong on someone else's screen.
import { describe, expect, it } from 'vitest';

import { hasBox, intersectsViewport, placeCard } from './placement';

const VP = { width: 1400, height: 900 };
const CARD = { width: 320, height: 160 };
const rect = (x: number, y: number, w = 200, h = 80) => ({ x, y, width: w, height: h });

describe('placeCard', () => {
  it('prefers below for an element near the top', () => {
    const p = placeCard(rect(600, 100), CARD, VP);
    expect(p.side).toBe('bottom');
    expect(p.y).toBeGreaterThan(100);
  });

  it('flips above when there is no room below', () => {
    const p = placeCard(rect(600, 820), CARD, VP);
    expect(p.side).toBe('top');
    expect(p.y + CARD.height).toBeLessThanOrEqual(820);
  });

  it('never places the card off the left or right edge', () => {
    for (const x of [0, 4, 1380, 1399]) {
      const p = placeCard(rect(x, 400), CARD, VP);
      expect(p.x).toBeGreaterThanOrEqual(0);
      expect(p.x + CARD.width).toBeLessThanOrEqual(VP.width);
    }
  });

  it('centres when there is nothing to point at', () => {
    const p = placeCard(null, CARD, VP);
    expect(p.side).toBe('center');
    expect(p.x).toBeCloseTo((VP.width - CARD.width) / 2, 0);
  });

  it('pins to the top margin when the card is taller than the viewport', () => {
    // The inverted-clamp case: a short landscape phone. Without the Math.max
    // guards the clamp range inverts and the card lands off-screen entirely.
    const tall = { width: 320, height: 700 };
    const p = placeCard(rect(300, 100), tall, { width: 900, height: 420 });
    expect(p.y).toBeGreaterThanOrEqual(0);
    expect(p.x).toBeGreaterThanOrEqual(0);
  });

  it('pins to the bottom on a narrow screen rather than flipping around', () => {
    const p = placeCard(rect(40, 120, 300, 60), CARD, { width: 390, height: 844 });
    expect(p.y + CARD.height).toBeLessThanOrEqual(844);
    expect(p.x).toBeGreaterThanOrEqual(0);
  });

  it('moves off the bottom when the element itself is down there', () => {
    const low = placeCard(rect(40, 700, 300, 100), CARD, { width: 390, height: 844 });
    expect(low.side).toBe('top');
  });
});

describe('target predicates', () => {
  it('rejects a zero-size box', () => {
    // display:none, or present but not yet laid out.
    expect(hasBox({ x: 10, y: 10, width: 0, height: 0 })).toBe(false);
    expect(hasBox(null)).toBe(false);
    expect(hasBox(rect(0, 0))).toBe(true);
  });

  it('rejects the mobile drawer parked off-canvas', () => {
    // A real 300x800 box at x = -300, courtesy of translateX(-100%). Sized and
    // present, so hasBox passes — which is why on-screen is a separate question.
    expect(intersectsViewport({ x: -300, y: 0, width: 300, height: 800 }, VP)).toBe(false);
  });

  it('accepts an element straddling an edge', () => {
    expect(intersectsViewport({ x: -20, y: 0, width: 300, height: 80 }, VP)).toBe(true);
  });
});
