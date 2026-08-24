// The agent gateway's transport, as a plain class with no React in it.
//
// WHY NOT `useEventStream`
// ------------------------
// That hook is an EventSource: one direction, no request/response correlation,
// and a reconnect that silently drops whatever was in flight. This surface needs
// the opposite of all three — outbound calls with ids, a pending-promise map,
// per-call timeouts, and a subscription lifecycle that survives a reconnect.
//
// The critical one is the third. A JSON-RPC client that does not REJECT its
// pending promises when the socket closes leaves `chat.send` awaiting forever,
// which is the same stuck-`busy` bug `useChat` already carries a try/finally to
// paper over. Here it is prevented rather than survived.
//
// SPLIT TRANSPORT, on purpose
// ---------------------------
// Events arrive over the socket; REQUESTS go over `POST /api/gateway/rpc`. So a
// dropped socket can never lose a mutation, and every side-effecting call
// funnels through one place the bridge can audit. That is a deliberate asymmetry
// and not an oversight — see `ava_bridge/gateway_api.py`.
//
// No React, so vitest can drive the whole thing against a fake socket.

// 'unconfigured' is distinct from 'down': the runtime has no gateway control
// plane at all (a Direct-floor install), as opposed to one that is configured
// but unreachable. The server sends it and then CLOSES the socket, so it must
// be sticky (see onclose) — otherwise the client would report "reconnecting"
// and redial on the backoff forever against a runtime that will never answer.
export type GatewayPhase = 'connecting' | 'open' | 'closed' | 'down' | 'unconfigured';

export interface GatewayEvent {
  topic: string;
  seq: number | null;
  payload: unknown;
}

export class GatewayClosedError extends Error {
  code = 'agent_down';
  constructor(message = 'the connection to the agent closed') {
    super(message);
    this.name = 'GatewayClosedError';
  }
}

export class GatewayCallError extends Error {
  code: string;
  gwCode: string;
  detail: unknown;
  retryable: boolean;
  constructor(body: {
    message?: string; error_code?: string; gw_code?: string;
    detail?: unknown; retryable?: boolean;
  }) {
    super(body.message || body.error_code || 'the agent refused the call');
    this.name = 'GatewayCallError';
    this.code = body.error_code || 'gateway_rpc_failed';
    this.gwCode = body.gw_code || '';
    this.detail = body.detail;
    this.retryable = !!body.retryable;
  }
}

type EventHandler = (ev: GatewayEvent) => void;
type StatusHandler = (phase: GatewayPhase, why: string) => void;

// The same 1s -> 15s curve `useEventStream` uses. One reconnect vocabulary in
// the product beats two that are almost the same.
const BACKOFF_MIN = 1000;
const BACKOFF_MAX = 15000;
const CALL_TIMEOUT_MS = 30_000;

export class GatewayClient {
  private ws: WebSocket | null = null;
  private url = '';
  private stopped = false;
  private retry = BACKOFF_MIN;
  private timer = 0;
  private phase: GatewayPhase = 'closed';
  private why = '';
  private lastSeq: number | null = null;

  // Topic -> handlers. Kept across reconnects and replayed on open, because a
  // panel that mounted while the socket was down must not have to know that.
  private subs = new Map<string, Set<EventHandler>>();
  private statusHandlers = new Set<StatusHandler>();
  private pending = new Set<{ reject: (e: Error) => void }>();

  connect(url = '/ws/gateway'): void {
    this.url = url;
    this.stopped = false;
    this.open();
  }

  close(): void {
    this.stopped = true;
    window.clearTimeout(this.timer);
    this.ws?.close();
    this.ws = null;
    this.failPending(new GatewayClosedError('the client was closed'));
    this.setPhase('closed', '');
  }

  status(): { phase: GatewayPhase; why: string } {
    return { phase: this.phase, why: this.why };
  }

  // ---- requests ----------------------------------------------------------
  /**
   * One control-plane call, over HTTP rather than the socket.
   *
   * A gateway-level failure arrives as an HTTP 200 body with `ok: false` — the
   * same convention `internal._told()` uses — because `lib/api.ts` maps a
   * bodyless 404 to `bridge_outdated` and a caller that cannot see the body
   * cannot show the owner the fix.
   */
  async call<T = unknown>(
    method: string,
    params: Record<string, unknown> = {},
    opts: { timeoutMs?: number; idempotencyKey?: string } = {},
  ): Promise<T> {
    const ctl = new AbortController();
    const t = window.setTimeout(() => ctl.abort(),
      opts.timeoutMs ?? CALL_TIMEOUT_MS);
    const entry = { reject: () => ctl.abort() };
    this.pending.add(entry);
    try {
      const r = await fetch('/api/gateway/rpc', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        credentials: 'same-origin',
        signal: ctl.signal,
        body: JSON.stringify({
          method,
          params,
          ...(opts.idempotencyKey ? { idempotency_key: opts.idempotencyKey } : {}),
          ...(opts.timeoutMs ? { timeout: opts.timeoutMs / 1000 } : {}),
        }),
      });
      if (r.status === 401) { window.location.href = '/login'; throw new GatewayClosedError(); }
      const body = await r.json().catch(() => ({}));
      if (!r.ok && !body?.error_code) {
        throw new GatewayCallError({ message: body?.error || `HTTP ${r.status}` });
      }
      if (body?.ok === false) throw new GatewayCallError(body);
      return body?.payload as T;
    } finally {
      window.clearTimeout(t);
      this.pending.delete(entry);
    }
  }

  // ---- events ------------------------------------------------------------
  /** Subscribe to one topic. Returns the unsubscribe. */
  subscribe(topic: string, onEvent: EventHandler): () => void {
    let set = this.subs.get(topic);
    if (!set) { set = new Set(); this.subs.set(topic, set); }
    set.add(onEvent);
    this.sendTopics();
    return () => {
      set!.delete(onEvent);
      if (!set!.size) this.subs.delete(topic);
      this.sendTopics();
    };
  }

  onStatus(cb: StatusHandler): () => void {
    this.statusHandlers.add(cb);
    cb(this.phase, this.why);
    return () => { this.statusHandlers.delete(cb); };
  }

  // ---- socket ------------------------------------------------------------
  private open(): void {
    if (this.stopped) return;
    this.setPhase('connecting', '');
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${window.location.host}${this.url}`);
    this.ws = ws;

    ws.onopen = () => {
      this.retry = BACKOFF_MIN;
      this.setPhase('open', '');
      // Replay the whole subscription set. A panel that mounted while the
      // socket was down subscribed into a map, not into a connection.
      this.sendTopics();
      if (this.lastSeq !== null) this.send({ op: 'resume', after: this.lastSeq });
    };

    ws.onmessage = (e) => {
      let frame: Record<string, unknown>;
      try { frame = JSON.parse(e.data as string); } catch { return; }
      this.handle(frame);
    };

    ws.onclose = () => {
      this.ws = null;
      // A runtime with no control plane sent 'unconfigured' and hung up. There
      // is nothing to reconnect to — redialing would loop on the backoff
      // forever — so keep the phase and stop. It only changes when the config
      // does, which re-mounts the client.
      if (this.phase === 'unconfigured') return;
      // NOT an error for in-flight work: a run very likely survived this, and
      // the reconnect reconciles. Callers are told the phase, not a failure.
      this.setPhase('down', 'reconnecting');
      if (this.stopped) return;
      this.timer = window.setTimeout(() => this.open(), this.retry);
      this.retry = Math.min(this.retry * 2, BACKOFF_MAX);
    };

    ws.onerror = () => { /* onclose always follows; one handler is enough */ };
  }

  private handle(frame: Record<string, unknown>): void {
    const op = String(frame.op || '');
    if (op === 'event') {
      const seq = typeof frame.seq === 'number' ? frame.seq : null;
      if (seq !== null) this.lastSeq = seq;
      this.emit({ topic: String(frame.topic || ''), seq, payload: frame.payload });
    } else if (op === 'gap' || op === 'dropped') {
      // Frames were lost. Say so as an event rather than papering over it —
      // consumers refetch instead of rendering a list with a hole in it.
      this.emit({ topic: 'ava.gateway.gap', seq: null, payload: frame });
    } else if (op === 'state') {
      const p: GatewayPhase = frame.phase === 'ready' ? 'open'
        : frame.phase === 'unconfigured' ? 'unconfigured' : 'down';
      this.setPhase(p, String(frame.why || ''));
    }
  }

  private emit(ev: GatewayEvent): void {
    for (const h of this.subs.get(ev.topic) || []) h(ev);
    for (const h of this.subs.get('*') || []) h(ev);
  }

  private sendTopics(): void {
    const topics = [...this.subs.keys()].filter((t) => t !== '*');
    this.send({ op: 'subscribe', topics });
  }

  private send(msg: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  private setPhase(phase: GatewayPhase, why: string): void {
    if (phase === this.phase && why === this.why) return;
    this.phase = phase;
    this.why = why;
    for (const h of this.statusHandlers) h(phase, why);
  }

  private failPending(err: Error): void {
    for (const p of this.pending) p.reject(err);
    this.pending.clear();
  }
}
