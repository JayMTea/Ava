import {
  createContext, createElement, useContext, useEffect, useRef, useState,
  type ReactNode,
} from 'react';
import { GatewayClient, type GatewayEvent, type GatewayPhase } from '../lib/gatewayClient';

// One client for the whole app, mounted ABOVE the router in main.tsx.
//
// Above, not inside the Agent view, for three reasons that all bite:
//   * it must survive a tab switch — reconnecting because somebody looked at
//     Vitals would drop every subscription and re-handshake for nothing,
//   * `useChat` needs it from the Chats tab, which is a different view,
//   * a socket per mounting component is a socket per StrictMode double-mount.

const Ctx = createContext<GatewayClient | null>(null);

export function GatewayProvider({ children }: { children: ReactNode }) {
  // Created once. A `useState` initialiser rather than `useMemo`, which React
  // is explicitly allowed to discard and recompute.
  const [client] = useState(() => new GatewayClient());
  useEffect(() => {
    client.connect();
    return () => client.close();
  }, [client]);
  return createElement(Ctx.Provider, { value: client }, children);
}

/** The client, or null outside a provider (which is a bug, not a state). */
export function useGateway(): GatewayClient | null {
  return useContext(Ctx);
}

export function useGatewayStatus(): { phase: GatewayPhase; why: string } {
  const client = useGateway();
  const [st, setSt] = useState<{ phase: GatewayPhase; why: string }>(
    () => client?.status() ?? { phase: 'closed', why: 'no gateway client' },
  );
  useEffect(() => {
    if (!client) return;
    return client.onStatus((phase, why) => setSt({ phase, why }));
  }, [client]);
  return st;
}

/**
 * Subscribe to one topic for the life of the component.
 *
 * `handler` is kept in a ref and the effect depends only on `topic`, matching
 * the idiom `OpsView` already uses for its SSE handlers: a handler that closes
 * over changing state would otherwise re-subscribe on every render, and the
 * gateway would see a subscribe storm instead of one subscription.
 */
export function useGatewaySubscription(
  topic: string | null,
  handler: (ev: GatewayEvent) => void,
): void {
  const client = useGateway();
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => {
    if (!client || !topic) return;
    return client.subscribe(topic, (ev) => ref.current(ev));
  }, [client, topic]);
}

/**
 * A control-plane call with `busy` and the last error, for panels.
 *
 * Shaped deliberately like `hub/hooks.ts`'s `useAction` — same `busy`, same
 * "the error is a string you render, not an exception you catch" contract — so
 * an Agent panel reads like a Setup panel to anyone who has seen one.
 */
export function useGatewayCall() {
  const client = useGateway();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [code, setCode] = useState('');

  async function run<T>(method: string, params: Record<string, unknown> = {},
                        opts?: { idempotencyKey?: string }): Promise<T | null> {
    if (!client) { setError('no gateway client'); return null; }
    setBusy(true);
    setError('');
    setCode('');
    try {
      return await client.call<T>(method, params, opts);
    } catch (e) {
      const err = e as { message?: string; code?: string };
      setError(err.message || String(e));
      // The code is what `fixes.ts` resolves a fix link from, by PATTERN — so a
      // panel gets a working "here is how to fix it" with no extra wiring.
      setCode(err.code || '');
      return null;
    } finally {
      setBusy(false);
    }
  }

  return { busy, error, code, run, setError };
}
