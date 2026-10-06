// "Ava cannot answer right now", shown wherever the owner is.
//
// It lives in App.tsx rather than inside Setup because the person who needs it
// is sitting in Chat wondering why nothing works. Every existing banner renders
// inside HubView, so a user who never opens Setup sees none of them.
//
// Not dismissible, deliberately. Every banner in this codebase is derived from
// live state and self-hides when the condition clears; nothing anywhere persists
// a dismissal. The condition IS the dismissal — and letting a novice hide the
// one thing explaining why the product does not respond would be a poor trade.
import { useEffect, useRef, useState } from 'react';

import { hub } from './hub/hubApi';
import { fixForCode } from '../lib/fixes';
import { bannerView, type InferenceHealth } from './inferenceView';

/** Slow poll. This is a background truth, not a live meter: the states it
 *  reports (no model configured, engine still starting) change on the order of
 *  minutes, and a warming vLLM resolves itself without the owner doing
 *  anything — which is the case this interval exists to catch. */
const POLL_MS = 20000;

export default function InferenceBanner() {
  const [health, setHealth] = useState<InferenceHealth | null>(null);

  useEffect(() => {
    let alive = true;
    const check = () => hub.inference()
      .then((r) => { if (alive) setHealth(r); })
      // A failed check is not a failed install. Staying null keeps the banner
      // hidden rather than announcing our own fetch problem to the owner.
      .catch(() => { /* leave the last known state alone */ });
    check();
    const t = setInterval(check, POLL_MS);
    // Re-check the moment a turn fails: the poll would otherwise take up to 20s
    // to agree with the error the user is already looking at.
    const onFail = () => check();
    window.addEventListener('ava:turn-failed', onFail);
    return () => {
      alive = false;
      clearInterval(t);
      window.removeEventListener('ava:turn-failed', onFail);
    };
  }, []);

  const view = bannerView(health);
  const ref = useRef<HTMLDivElement | null>(null);

  // Publish our measured height so the shell can shift out from under us.
  //
  // The banner is position:fixed — it reserves no space — so without this it
  // paints over the header and the top of every view. The height cannot be a
  // constant: the text comes from the server and wraps to two lines under
  // 760px, so it is observed rather than assumed. Removing the property on
  // hide/unmount is what lets the CSS fall back to 0px, so the layout returns
  // by itself the moment the condition clears.
  //
  // Runs BEFORE the early return below, because a hook cannot live after one.
  useEffect(() => {
    const root = document.documentElement;
    const clear = () => root.style.removeProperty('--inf-banner-h');
    const el = ref.current;
    if (!view.show || !el) { clear(); return clear; }
    const set = () => root.style.setProperty(
      '--inf-banner-h', `${Math.ceil(el.getBoundingClientRect().height)}px`);
    set();
    if (typeof ResizeObserver === 'undefined') return clear;
    const ro = new ResizeObserver(set);
    ro.observe(el);
    return () => { ro.disconnect(); clear(); };
  }, [view.show, view.text, view.code]);

  if (!view.show) return null;
  const fix = fixForCode(view.code);

  return (
    <div ref={ref} className={`inf-banner${view.role === 'alert' ? ' err' : ''}`} role={view.role}>
      <span className="inf-banner-text">{view.text}</span>
      {fix && <a className="inf-banner-fix" href={`#${fix.hash}`}>{fix.label} →</a>}
    </div>
  );
}
