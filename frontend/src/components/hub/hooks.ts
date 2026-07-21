import { useCallback, useEffect, useState } from 'react';

// Shared Hub data/action hooks — every Setup panel used to hand-roll the same
// load/error/busy/message dance (33 nullable-state holders, 10 load callbacks,
// and ~16 hardcoded `hub-msg err` renders that showed SUCCESS text in red).
// These centralise it so a panel declares intent, not plumbing.

/** A one-shot result message with a tone. `ok:false` renders red, `ok:true` green. */
export type ActionMsg = { text: string; ok: boolean } | null;

/**
 * Load a resource on mount (and whenever `deps` change), exposing the same
 * `{ data, error, loading, reload, setData }` every panel needs. `setData` is
 * returned so callers can still patch state optimistically (pins, toggles).
 */
export function useResource<T>(fetcher: () => Promise<T>, deps: React.DependencyList = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  // The fetcher closes over `deps`; we intentionally key the callback on those,
  // not on the (always-new) fetcher identity — the standard load-hook pattern.
  const reload = useCallback(() => {
    setLoading(true); setError('');
    fetcher()
      .then((d) => setData(d))
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => { reload(); }, [reload]);
  return { data, error, loading, reload, setData };
}

/**
 * Run an async panel action with managed `busy` + a tone-aware result message.
 * The action returns an error string (falsy = success) or throws; on success it
 * shows `okText` (green) if given, otherwise nothing. This makes it impossible
 * to accidentally render a success message with the error tone.
 *
 *   const act = useAction();
 *   act.run(async () => {
 *     const r = await hub.setApproval(mode);
 *     if (r.error) return r.error;      // → red
 *     setSys(...); onRestart();          // success side-effects
 *   }, 'Saved — in effect now.');        // → green
 */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<ActionMsg>(null);

  const run = useCallback(async (
    fn: () => Promise<string | null | undefined | void>,
    okText?: string,
  ) => {
    setBusy(true); setMessage(null);
    try {
      const err = await fn();
      if (err) setMessage({ text: err, ok: false });
      else if (okText) setMessage({ text: okText, ok: true });
    } catch (e) {
      setMessage({ text: (e as Error).message, ok: false });
    } finally {
      setBusy(false);
    }
  }, []);

  return { busy, message, setMessage, run };
}
