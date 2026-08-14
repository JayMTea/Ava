// Geometry for the sidebar's "Settings & dashboards" flyout. Pure, so it is
// testable — the SPA has no component-render harness (see tour/placement.test.ts
// and hub/provisionView.test.ts, which exist for the same reason), and the two
// cases that actually bite only happen on someone else's screen.
import { describe, expect, it } from 'vitest';

import { EDGE_PAD, placePanel, placeRail } from './flyoutPlace';

/** The panel foot's trigger: a full-width row in the 300px sidebar. */
const row = (top: number, left = 8, width = 284) =>
  ({ left, right: left + width, top, bottom: top + 40, width });

describe('placePanel', () => {
  it('matches the trigger left edge and width', () => {
    const p = placePanel(row(852), { width: 1400, height: 900 });
    expect(p.left).toBe(8);
    expect(p.width).toBe(284);
  });

  it('opens upward: the menu sits above the trigger, never over it', () => {
    const vp = { width: 1400, height: 900 };
    const p = placePanel(row(852), vp);
    expect(vp.height - p.bottom).toBeLessThanOrEqual(852);
  });

  it('never lets the menu grow off the top of a short viewport', () => {
    // The inverted case, and the reason maxHeight exists: `bottom` is pinned, so
    // an unclamped height escapes the TOP and hides the FIRST item — not the
    // last, which is where anyone would think to look.
    const vp = { width: 390, height: 120 };
    const p = placePanel(row(72), vp);
    const top = vp.height - p.bottom - (p.maxHeight ?? 0);
    expect(top).toBeGreaterThanOrEqual(0);
    expect(p.maxHeight).toBeGreaterThan(0);
  });

  it('clamps a trigger parked off-screen by the closed mobile drawer', () => {
    // translateX(-100%) leaves a real, laid-out 300px box at x = -300.
    const p = placePanel(row(700, -292), { width: 390, height: 800 });
    expect(p.left).toBeGreaterThanOrEqual(EDGE_PAD);
  });
});

describe('placeRail', () => {
  it('opens to the right of the trigger, bottom edges level', () => {
    const p = placeRail(
      { left: 8, right: 48, top: 800, bottom: 840, width: 40 },
      { width: 1400, height: 900 },
    );
    expect(p.left).toBe(56);
    expect(p.bottom).toBe(60);
  });
});
