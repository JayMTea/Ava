import { useCallback, useEffect, useState } from 'react';

/**
 * A page's sub-tab, kept in the URL hash under a fixed prefix.
 *
 * The Data page held its tab in plain `useState`, so a reload dropped you back
 * on Overview and nothing could link INTO a tab — which is what made
 * "Setup → History moved to Data" impossible to redirect properly.
 *
 * This is the simple case: one segment, no legacy map. Setup needs neither —
 * it has two segments and a table of retired addresses, so its arithmetic
 * lives in components/hub/hubRoute.ts where vitest can cover it.
 *
 * Safe to nest under App.tsx's router because `viewFromHash` reads only the
 * FIRST segment (`split('/')[0]`) and its stamping effect returns early when
 * that segment already matches — so anything past it survives untouched.
 *
 * `fallback` keeps its URL bare (plain `#data`), so the default never shows a
 * redundant segment for someone to hand-edit wrongly later.
 */
export function useHashTab<T extends string>(
  prefix: string, ids: readonly T[], fallback: T,
): [T, (t: T) => void] {
  const read = useCallback((): T => {
    if (typeof window === 'undefined') return fallback;
    const h = window.location.hash.replace(/^#\/?/, '');
    if (h !== prefix && !h.startsWith(`${prefix}/`)) return fallback;
    const seg = h.slice(prefix.length + 1).split('/')[0];
    return (ids as readonly string[]).includes(seg) ? (seg as T) : fallback;
  }, [prefix, ids, fallback]);

  const [tab, setTabState] = useState<T>(read);

  const setTab = useCallback((t: T) => {
    setTabState(t);
    const next = t === fallback ? prefix : `${prefix}/${t}`;
    if (window.location.hash.replace(/^#\/?/, '') !== next) window.location.hash = next;
  }, [prefix, fallback]);

  // Back/forward and hand-edited URLs move the tab too.
  useEffect(() => {
    const onHash = () => setTabState(read());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, [read]);

  return [tab, setTab];
}
