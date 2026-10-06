import { useCallback, useEffect, useRef, useState } from 'react';

// Shared Hub data/action hooks — every Setup panel used to hand-roll the same
// load/error/busy/message dance (33 nullable-state holders, 10 load callbacks,
// and ~16 hardcoded `hub-msg err` renders that showed SUCCESS text in red).
// These centralise it so a panel declares intent, not plumbing.

/** A one-shot result message with a tone. `ok:false` renders red, `ok:true` green. */
export type ActionMsg = { text: string; ok: boolean } | null;

/**
 * Load a resource on mount (and whenever `deps` change), exposing the same
 * `{ data, error, code, loading, reload, setData }` every panel needs.
 * `setData` is returned so callers can still patch state optimistically (pins,
 * toggles).
 *
 * `code` is the failure's machine-readable code when it has one (`lib/api.req`
 * sets it from the backend's `error_code`, or to `bridge_outdated` when the
 * running bridge has no such route). Kept alongside the message because some
 * failures are not faults in the thing being loaded and must not be rendered
 * as one — see <ResourceError>.
 */
export function useResource<T>(fetcher: () => Promise<T>, deps: React.DependencyList = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [code, setCode] = useState('');
  const [loading, setLoading] = useState(true);
  const request = useRef(0);

  // The fetcher closes over `deps`; we intentionally key the callback on those,
  // not on the (always-new) fetcher identity — the standard load-hook pattern.
  const reload = useCallback(async () => {
    const id = ++request.current;
    setLoading(true); setError(''); setCode('');
    try {
      const d = await fetcher();
      if (id === request.current) setData(d);
    } catch (e) {
      if (id === request.current) {
        setError((e as Error).message);
        setCode((e as { code?: string }).code || '');
      }
    } finally {
      if (id === request.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    void reload();
    return () => { request.current += 1; };
  }, [reload]);
  return { data, error, code, loading, reload, setData };
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
