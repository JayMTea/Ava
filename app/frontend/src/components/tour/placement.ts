// Pure geometry for the walkthrough card. No DOM, no React.
//
// Split out because this is the part with real cases — flip, clamp, mobile, an
// oversized card — and the SPA has no component-render harness (see
// components/hub/provisionView.test.ts, which exists for exactly this reason).
// Geometry that cannot be unit-tested is geometry that is wrong on someone
// else's screen.

export type Side = 'top' | 'bottom' | 'left' | 'right';
export type Placement = Side | 'auto';

export interface Rect { x: number; y: number; width: number; height: number }
export interface Viewport { width: number; height: number }
export interface Placed { x: number; y: number; side: Side | 'center' }

export const PAD = 6;        // breathing room around the lit element
export const GAP = 12;       // hole edge to card
export const MARGIN = 12;    // card to viewport edge
export const MOBILE_W = 760; // matches every @media breakpoint in the repo

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

/** A real, laid-out box. Rejects display:none and not-yet-laid-out elements. */
export const hasBox = (r: Rect | null | undefined): r is Rect =>
  !!r && r.width > 0 && r.height > 0;

/** On-screen at all. Rejects the mobile drawer, which is a real 300x800 box
 *  parked at x = -300 by translateX(-100%) — present, sized, and unusable. */
export function intersectsViewport(r: Rect, vp: Viewport): boolean {
  return r.x < vp.width && r.y < vp.height && r.x + r.width > 0 && r.y + r.height > 0;
}

export function inflate(r: Rect, pad: number): Rect {
  return { x: r.x - pad, y: r.y - pad, width: r.width + pad * 2, height: r.height + pad * 2 };
}

const ORDER: Record<Side, Side[]> = {
  bottom: ['bottom', 'top', 'right', 'left'],
  top: ['top', 'bottom', 'right', 'left'],
  right: ['right', 'left', 'bottom', 'top'],
  left: ['left', 'right', 'bottom', 'top'],
};

function originFor(side: Side, h: Rect, c: { width: number; height: number }) {
  switch (side) {
    case 'bottom': return { x: h.x + h.width / 2 - c.width / 2, y: h.y + h.height + GAP };
    case 'top': return { x: h.x + h.width / 2 - c.width / 2, y: h.y - GAP - c.height };
    case 'right': return { x: h.x + h.width + GAP, y: h.y + h.height / 2 - c.height / 2 };
    case 'left': return { x: h.x - GAP - c.width, y: h.y + h.height / 2 - c.height / 2 };
  }
}

/** Where to put the card. Returns viewport coordinates. */
export function placeCard(
  target: Rect | null,
  card: { width: number; height: number },
  vp: Viewport,
  preferred: Placement = 'auto',
): Placed {
  const box = {
    left: MARGIN, top: MARGIN,
    right: vp.width - MARGIN, bottom: vp.height - MARGIN,
  };
  // These guards are load-bearing, not defensive noise: when the card is taller
  // than the usable box (a short landscape phone) the naive clamp range inverts
  // and returns nonsense, putting the card off-screen entirely.
  const fitX = (x: number) => clamp(x, box.left, Math.max(box.left, box.right - card.width));
  const fitY = (y: number) => clamp(y, box.top, Math.max(box.top, box.bottom - card.height));

  if (!hasBox(target)) {
    return { x: fitX((vp.width - card.width) / 2), y: fitY((vp.height - card.height) / 2), side: 'center' };
  }

  const h = inflate(target, PAD);

  // Narrow screens: no room to flip around an element. Pin to the bottom, or the
  // top when the element itself is down there.
  if (vp.width <= MOBILE_W) {
    const bottomY = box.bottom - card.height;
    const collides = h.y + h.height > bottomY;
    const y = collides && h.y > card.height + GAP + box.top ? box.top : bottomY;
    return { x: fitX((vp.width - card.width) / 2), y: fitY(y), side: collides ? 'top' : 'bottom' };
  }

  const order = preferred === 'auto' ? ORDER.bottom : ORDER[preferred];
  for (const side of order) {
    const o = originFor(side, h, card);
    const inside = o.x >= box.left && o.y >= box.top
      && o.x + card.width <= box.right && o.y + card.height <= box.bottom;
    if (inside) return { x: o.x, y: o.y, side };
  }

  // Nothing fits: take the side whose clamped card covers the least of the thing
  // it is pointing at, so the card never sits on top of its own subject.
  let best: Placed | null = null;
  let bestOverlap = Infinity;
  for (const side of order) {
    const o = originFor(side, h, card);
    const x = fitX(o.x);
    const y = fitY(o.y);
    const ox = Math.max(0, Math.min(x + card.width, h.x + h.width) - Math.max(x, h.x));
    const oy = Math.max(0, Math.min(y + card.height, h.y + h.height) - Math.max(y, h.y));
    const overlap = ox * oy;
    if (overlap < bestOverlap) { bestOverlap = overlap; best = { x, y, side }; }
  }
  return best!;
}
