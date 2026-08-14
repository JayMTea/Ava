// Pure geometry for the sidebar's "Settings & dashboards" flyout. No DOM, no
// React — the same split, for the same reason, as components/tour/placement.ts:
// the SPA has no component-render harness, and the cases that actually bite (a
// short viewport, the mobile drawer parked off-canvas) are only reachable from a
// unit test. Geometry that cannot be unit-tested is geometry that is wrong on
// someone else's screen.

/** The subset of DOMRect this needs, so a test can pass a plain object. */
export interface Rect { left: number; right: number; top: number; bottom: number; width: number }
export interface Viewport { width: number; height: number }

/** A position:fixed style object. Both variants pin `bottom`, never `top`,
 *  because both grow UPWARD: anchoring the bottom edge welds the menu to its
 *  trigger as the item list changes length, instead of letting it slide down. */
export interface Placed { left: number; bottom: number; width?: number; maxHeight?: number }

export const RAIL_GAP = 8;   // rail button → menu, horizontal
export const PANEL_GAP = 6;  // panel row → menu, vertical
export const EDGE_PAD = 8;   // menu → viewport edge

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

/** Collapsed rail: up and to the RIGHT, bottom edge level with the trigger's,
 *  natural width. What RailFlyout did inline before this module existed. */
export function placeRail(r: Rect, vp: Viewport): Placed {
  return { left: r.right + RAIL_GAP, bottom: vp.height - r.bottom };
}

/** Expanded panel: a lid on the trigger. Same left edge, same width, growing
 *  upward — it stays inside the 300px sidebar column instead of reaching out
 *  over the chat canvas, so it reads as the row unfolding rather than as a
 *  popover that happened to land nearby.
 *
 *  maxHeight is the load-bearing line. The trigger is pinned to the bottom of a
 *  full-height column, so the room above it is ~the whole viewport and four
 *  items fit every realistic time. The exception is a short window — a landscape
 *  phone with the keyboard up, a dragged-short desktop window. Without the clamp
 *  the menu keeps its natural height and, because `bottom` is pinned, grows off
 *  the TOP of the screen: the FIRST item becomes unreachable, which is the
 *  failure nobody looks for. Pairs with overflow-y:auto on .panel-menu.
 *
 *  `left` is clamped for the other one. An element inside the closed mobile
 *  drawer is a real, laid-out box parked at x = -300 by translateX(-100%) — see
 *  tour/placement.ts intersectsViewport, which exists for that same box. Drawer
 *  dismisses the menu when the sidebar collapses; this is the second lock. */
export function placePanel(r: Rect, vp: Viewport): Placed {
  const width = Math.round(r.width);
  return {
    left: Math.round(clamp(r.left, EDGE_PAD, Math.max(EDGE_PAD, vp.width - width - EDGE_PAD))),
    width,
    bottom: Math.round(vp.height - r.top + PANEL_GAP),
    maxHeight: Math.max(Math.round(r.top - PANEL_GAP - EDGE_PAD), 0),
  };
}
