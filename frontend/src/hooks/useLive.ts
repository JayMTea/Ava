import { useCallback, useEffect, useRef, useState } from 'react';

// Poll a resource on an interval, pausing while the tab is hidden and refetching
// immediately when it becomes visible again. Errors are surfaced, not thrown.
export function useLiveResource<T>(fetcher: () => Promise<T>, intervalMs = 4000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const fRef = useRef(fetcher);
  fRef.current = fetcher;

  const load = useCallback(async () => {
    try {
      const d = await fRef.current();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e as Error);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let timer = 0;
    const tick = async () => {
      if (!document.hidden) await load();
      timer = window.setTimeout(tick, intervalMs);
    };
    load();
    timer = window.setTimeout(tick, intervalMs);
    const onVis = () => { if (!document.hidden) load(); };
    document.addEventListener('visibilitychange', onVis);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVis);
    };
  }, [load, intervalMs]);

  return { data, error, loading, refresh: load };
}

// Subscribe to a Server-Sent Events endpoint with auto-reconnect (exponential
// backoff). `handlers` maps event names to callbacks receiving parsed JSON.
export function useEventStream(
  url: string,
  handlers: Record<string, (data: unknown) => void>,
  enabled = true,
) {
  const hRef = useRef(handlers);
  hRef.current = handlers;

  useEffect(() => {
    if (!enabled) return;
    let es: EventSource | null = null;
    let stopped = false;
    let retry = 1000;
    let reconnectTimer = 0;

    const connect = () => {
      if (stopped) return;
      es = new EventSource(url, { withCredentials: true });
      es.onopen = () => { retry = 1000; };
      Object.keys(hRef.current).forEach((ev) => {
        es!.addEventListener(ev, (e: MessageEvent) => {
          try {
            hRef.current[ev](e.data ? JSON.parse(e.data) : null);
          } catch {
            /* ignore malformed frame */
          }
        });
      });
      es.onerror = () => {
        es?.close();
        if (!stopped) {
          reconnectTimer = window.setTimeout(connect, retry);
          retry = Math.min(retry * 2, 15000);
        }
      };
    };
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(reconnectTimer);
      es?.close();
    };
  }, [url, enabled]);
}
