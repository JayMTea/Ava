// Decides whether a page's walkthrough should run, and records that it did.
//
// Mounted once in App.tsx and driven by the current view. The seen-set is
// fetched once after first paint — never before, because orientation for a
// first-run user is not worth a millisecond of everyone else's boot.
//
// Skip ends THIS page only. A user who skips Vitals still meets Operations on
// its own terms, which is the point of tracking pages separately rather than
// keeping one "seen the tour" boolean.
import { useCallback, useEffect, useRef, useState } from 'react';

import { hub } from '../hub/hubApi';
import Spotlight from './Spotlight';
import { TOURS } from './steps';

export default function TourHost({ view }: { view: string }) {
  const [seen, setSeen] = useState<Set<string> | null>(null);   // null = not loaded
  const [page, setPage] = useState<string | null>(null);
  const [index, setIndex] = useState(0);
  // Pages closed in this session. Kept separately from `seen` so a failed POST
  // cannot make a dismissed walkthrough reappear on the next view change — the
  // user said no once and that has to hold regardless of the network.
  const closed = useRef<Set<string>>(new Set());

  useEffect(() => {
    let alive = true;
    const load = () => hub.tour()
      // Only a WELL-FORMED answer counts as loaded. `{ok:true}` with no `seen`
      // must stay unknown, never "seen nothing": demo/src/engine/mock.ts answers
      // every unmatched /api/* with exactly that, so a loose parse would paint
      // spotlights over every captured demo video and every Ava:verify run.
      .then((r) => { if (alive && Array.isArray(r?.seen)) setSeen(new Set(r.seen)); })
      // Unreadable state fails CLOSED. Re-showing a walkthrough someone already
      // dismissed is worse than never showing it.
      .catch(() => { /* stays null: nothing arms */ });
    load();
    // Replay lives in Setup and this component is mounted in App, so the button
    // tells us the same way the app already broadcasts connector changes
    // ('ava:apps-changed' in App.tsx). Without this, Replay does nothing until
    // the next reload.
    const again = () => { closed.current.clear(); setPage(null); load(); };
    window.addEventListener('ava:tour-reset', again);
    return () => { alive = false; window.removeEventListener('ava:tour-reset', again); };
  }, []);

  useEffect(() => {
    if (!seen) return;
    if (page) return;                       // one at a time
    if (!TOURS[view]?.length) return;
    if (seen.has(view) || closed.current.has(view)) return;
    setPage(view);
    setIndex(0);
  }, [view, seen, page]);

  // A view change while a walkthrough is open means the user got out some other
  // way (a fix-it link, the back button). Close it rather than describing a
  // screen that is no longer on-screen.
  useEffect(() => {
    if (page && view !== page) setPage(null);
  }, [view, page]);

  const finish = useCallback((p: string) => {
    closed.current.add(p);
    setPage(null);
    setSeen((s) => new Set(s).add(p));
    hub.tourSeen(p).catch(() => { /* recorded locally; retried next install */ });
  }, []);

  if (!page) return null;
  const steps = TOURS[page];
  if (!steps?.length) return null;
  const step = steps[Math.min(index, steps.length - 1)];

  return (
    <Spotlight
      step={step}
      index={index}
      total={steps.length}
      onBack={() => setIndex((i) => Math.max(0, i - 1))}
      onNext={() => (index + 1 >= steps.length ? finish(page) : setIndex((i) => i + 1))}
      onSkip={() => finish(page)}
    />
  );
}
