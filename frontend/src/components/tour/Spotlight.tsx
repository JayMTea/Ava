// The walkthrough overlay: dim the page, cut a hole around one element, float a
// card beside it.
//
// FIVE DIVS, NOT A MASK OR A BOX-SHADOW. Four rects tile the viewport around the
// target and a fifth transparent one covers the target itself. A
// `box-shadow: 0 0 0 9999px` spread is shorter and gives rounded corners free,
// but shadows are paint-only and are never hit-tested — and this overlay has to
// swallow clicks on the page underneath, or a user clicks a nav item mid-step
// and the walkthrough is describing a screen that is no longer there.
//
// A MISSING TARGET IS NORMAL. Panels legitimately render an empty state
// ("Nothing running right now."), data arrives after first paint, and on a phone
// several targets sit behind the rail flyout. So an unresolved selector is not an
// error: it degrades to a centered card carrying the same copy. Every step is
// written to read correctly either way, which is why no step says "this panel
// here".
//
// DO NOT copy InfoTip's dismissal block into this file. That popover closes on
// pointermove/pointerdown/scroll because a hover tip that wedges open is a bug.
// A walkthrough that vanished when the mouse moved would be unusable. The lesson
// that transfers is "never wedge with no way out", and here that means: Escape
// always skips, Skip renders in every state including the fallback, and the wait
// for a target is hard-bounded.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

import type { TourStep } from './steps';
import { PAD, hasBox, inflate, intersectsViewport, placeCard, type Rect } from './placement';

interface Props {
  step: TourStep;
  index: number;
  total: number;
  onNext: () => void;
  onBack: () => void;
  onSkip: () => void;
}

const TARGET_WAIT_MS = 1200;

/** Track a target's box, or null when there is nothing usable to point at. */
function useTargetRect(selector: string | undefined): Rect | null {
  const [rect, setRect] = useState<Rect | null>(null);

  useLayoutEffect(() => {
    setRect(null);
    if (!selector) return;
    let raf = 0;
    let last = '';
    let latched = false;               // gave up waiting -> stay centered
    const deadline = performance.now() + TARGET_WAIT_MS;

    // One rAF loop rather than scroll + resize + ResizeObserver + Mutation-
    // Observer. Those enumerate CAUSES; this observes the EFFECT, so it also
    // covers the ones nobody enumerated — the drawer's width transition, a
    // useResource swapping a skeleton for real rows, a chart reflowing, fonts
    // landing. The app scrolls inner containers rather than window, so a scroll
    // listener would have to be capture-phase on document for strictly less
    // coverage. Gated on a changed rect, so the steady state costs zero renders.
    const tick = () => {
      raf = requestAnimationFrame(tick);
      const el = document.querySelector(selector);
      // isConnected: a panel reload can swap the DOM under us, and tracking a
      // detached node means a frozen rect and a spotlight on empty space.
      const r = el && el.isConnected ? el.getBoundingClientRect() : null;
      const usable = hasBox(r) && intersectsViewport(r, {
        width: window.innerWidth, height: window.innerHeight,
      });
      if (!usable) {
        if (!latched && performance.now() > deadline) { latched = true; }
        if (latched) { if (last !== '') { last = ''; setRect(null); } }
        return;
      }
      latched = false;
      const key = `${Math.round(r!.x)}|${Math.round(r!.y)}|${Math.round(r!.width)}|${Math.round(r!.height)}`;
      if (key !== last) {
        last = key;
        setRect({ x: r!.x, y: r!.y, width: r!.width, height: r!.height });
      }
    };
    tick();
    return () => cancelAnimationFrame(raf);
  }, [selector]);

  return rect;
}

export default function Spotlight(
  { step, index, total, onNext, onBack, onSkip }: Props,
) {
  const rect = useTargetRect(step.target);
  const cardRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);

  // Make the dimmed page inert: removes it from the tab order, from hit testing
  // AND from the accessibility tree in one attribute. The four dim rects already
  // block the mouse; this is what makes the keyboard and a screen reader agree
  // with them. #root is the only non-portal child of body, and this overlay
  // portals to body, so it sits outside the inert subtree.
  useEffect(() => {
    const root = document.getElementById('root');
    root?.setAttribute('inert', '');
    return () => root?.removeAttribute('inert');
  }, []);

  // Focus the card on every step so a screen reader re-announces title and body.
  // preventScroll is not optional: focusing without it can scroll an ancestor
  // and move the very element we just measured.
  useEffect(() => {
    cardRef.current?.focus({ preventScroll: true });
  }, [index]);

  // Restore focus to wherever it was when the walkthrough opened.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    return () => opener?.focus?.();
  }, []);

  // Escape skips this page. Tab wraps inside the card — belt and braces for
  // browsers without `inert`.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); onSkip(); return; }
      if (e.key !== 'Tab') return;
      const btns = cardRef.current?.querySelectorAll<HTMLElement>('button:not([disabled])');
      if (!btns?.length) return;
      const first = btns[0];
      const last = btns[btns.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', onKey, true);
    return () => document.removeEventListener('keydown', onKey, true);
  }, [onSkip]);

  // Place the card once it and the target have been measured. Layout effect so
  // it lands before paint; the card is hidden until then.
  const reposition = useCallback(() => {
    const c = cardRef.current?.getBoundingClientRect();
    if (!c) return;
    setPos(placeCard(rect, { width: c.width, height: c.height },
                     { width: window.innerWidth, height: window.innerHeight }));
  }, [rect]);

  useLayoutEffect(() => { reposition(); }, [reposition, index]);
  useEffect(() => {
    const el = cardRef.current;
    const ro = el && 'ResizeObserver' in window ? new ResizeObserver(reposition) : null;
    if (ro && el) ro.observe(el);
    window.addEventListener('resize', reposition);
    return () => { ro?.disconnect(); window.removeEventListener('resize', reposition); };
  }, [reposition]);

  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const hole = rect ? inflate(rect, PAD) : null;

  const dim = (s: React.CSSProperties, key: string) => (
    <div key={key} className="tour-dim" style={s} aria-hidden="true" />
  );

  return createPortal(
    <div className="tour-root">
      {hole ? (
        <>
          {dim({ top: 0, left: 0, width: '100%', height: Math.max(0, hole.y) }, 't')}
          {dim({ top: hole.y, left: 0, width: Math.max(0, hole.x), height: hole.height }, 'l')}
          {dim({ top: hole.y, left: hole.x + hole.width,
                 width: Math.max(0, vw - hole.x - hole.width), height: hole.height }, 'r')}
          {dim({ top: hole.y + hole.height, left: 0, width: '100%',
                 height: Math.max(0, vh - hole.y - hole.height) }, 'b')}
          {/* Transparent, but still swallows clicks: the element is shown, not
              handed over. Advancing is Back/Next's job. */}
          <div className="tour-hole" aria-hidden="true"
               style={{ top: hole.y, left: hole.x, width: hole.width, height: hole.height }} />
        </>
      ) : (
        dim({ inset: 0 }, 'all')
      )}

      <div
        ref={cardRef}
        className="tour-card"
        style={pos ? { transform: `translate3d(${Math.round(pos.x)}px, ${Math.round(pos.y)}px, 0)` } : undefined}
        data-placed={pos ? '' : undefined}
        role="dialog"
        aria-modal="true"
        aria-labelledby="tour-title"
        aria-describedby="tour-body"
        tabIndex={-1}
      >
        <h3 id="tour-title">{step.title}</h3>
        <p id="tour-body">{step.body}</p>
        <div className="tour-foot">
          <span className="tour-count">{index + 1} of {total}</span>
          <div className="tour-btns">
            <button type="button" className="hub-btn ghost sm" onClick={onSkip}>Skip</button>
            {index > 0 && (
              <button type="button" className="hub-btn ghost sm" onClick={onBack}>Back</button>
            )}
            <button type="button" className="hub-btn sm" onClick={onNext}>
              {index + 1 === total ? 'Done' : 'Next'}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
