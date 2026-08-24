import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GatewayCallError, GatewayClient } from './gatewayClient';

// A WebSocket stand-in. The real failure modes here are about LIFECYCLE — what
// survives a reconnect, what is rejected on close — so the fake has to be
// driveable rather than merely present.
class FakeSocket {
  static last: FakeSocket | null = null;
  static opened = 0;
  static readonly OPEN = 1;

  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;

  constructor(public url: string) {
    FakeSocket.last = this;
    FakeSocket.opened += 1;
  }
  send(raw: string) { this.sent.push(raw); }
  close() { this.readyState = 3; this.onclose?.(); }

  open() { this.readyState = 1; this.onopen?.(); }
  deliver(frame: unknown) { this.onmessage?.({ data: JSON.stringify(frame) }); }
  frames() { return this.sent.map((s) => JSON.parse(s)); }
}

describe('GatewayClient — socket lifecycle', () => {
  beforeEach(() => {
    FakeSocket.last = null;
    FakeSocket.opened = 0;
    vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket);
    vi.stubGlobal('window', {
      ...globalThis.window,
      location: { protocol: 'http:', host: 'localhost:8096', href: '' },
      // Delegated lazily, NOT bound: `vi.useFakeTimers()` replaces the globals
      // after this stub is built, and a bound reference would keep calling the
      // real ones — so every reconnect test would sit waiting for wall-clock
      // time that the fake clock never advances.
      setTimeout: (...a: Parameters<typeof globalThis.setTimeout>) =>
        globalThis.setTimeout(...a),
      clearTimeout: (...a: Parameters<typeof globalThis.clearTimeout>) =>
        globalThis.clearTimeout(...a),
    });
  });
  afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

  it('connects to the live websocket route, not an /api path', () => {
    const c = new GatewayClient();
    c.connect();
    expect(FakeSocket.last!.url).toBe('ws://localhost:8096/ws/gateway');
    c.close();
  });

  it('reports connecting -> open -> down as the socket moves', () => {
    const seen: string[] = [];
    const c = new GatewayClient();
    c.onStatus((p) => seen.push(p));
    c.connect();
    FakeSocket.last!.open();
    FakeSocket.last!.close();
    expect(seen).toEqual(['closed', 'connecting', 'open', 'down']);
    c.close();
  });

  it('an unconfigured state frame is sticky and schedules no reconnect', () => {
    // The bridge sends {op:state, phase:unconfigured} then closes the socket for
    // a Direct-floor runtime. Without stickiness the client would report
    // "reconnecting" and redial on the backoff forever — a live bug.
    vi.useFakeTimers();
    const seen: string[] = [];
    const c = new GatewayClient();
    c.onStatus((p) => seen.push(p));
    c.connect();
    FakeSocket.last!.open();
    FakeSocket.last!.deliver({ op: 'state', phase: 'unconfigured', why: 'no gateway' });
    FakeSocket.last!.close();
    const openedAfter = FakeSocket.opened;
    vi.advanceTimersByTime(60_000);
    expect(c.status().phase).toBe('unconfigured');
    expect(FakeSocket.opened).toBe(openedAfter);      // no redial
    expect(seen).not.toContain('down');
    c.close();
  });

  it('a dropped socket is NOT reported as a failed run', () => {
    // The run very likely survived; the reconnect reconciles. Calling this an
    // error is how closing a laptop lid used to lose a three-minute turn.
    const c = new GatewayClient();
    let why = '';
    c.onStatus((_p, w) => { why = w; });
    c.connect();
    FakeSocket.last!.open();
    FakeSocket.last!.close();
    expect(why).toBe('reconnecting');
    c.close();
  });

  it('replays every subscription on reconnect', async () => {
    // A panel subscribes into a map, not into a connection — it must not have
    // to know the socket was down when it mounted.
    vi.useFakeTimers();
    const c = new GatewayClient();
    c.connect();
    const first = FakeSocket.last!;
    first.open();
    c.subscribe('run.step', () => {});
    c.subscribe('session.update', () => {});
    first.close();
    await vi.advanceTimersByTimeAsync(1100);
    const second = FakeSocket.last!;
    expect(second).not.toBe(first);
    second.open();
    const topics = second.frames().find((f) => f.op === 'subscribe')?.topics;
    expect(new Set(topics)).toEqual(new Set(['run.step', 'session.update']));
    c.close();
  });

  it('backs off 1s -> 2s -> 4s rather than hammering', async () => {
    vi.useFakeTimers();
    const c = new GatewayClient();
    c.connect();
    FakeSocket.last!.open();
    FakeSocket.last!.close();
    await vi.advanceTimersByTimeAsync(999);
    expect(FakeSocket.opened).toBe(1);
    await vi.advanceTimersByTimeAsync(2);
    expect(FakeSocket.opened).toBe(2);
    FakeSocket.last!.close();
    await vi.advanceTimersByTimeAsync(1999);
    expect(FakeSocket.opened).toBe(2);
    await vi.advanceTimersByTimeAsync(2);
    expect(FakeSocket.opened).toBe(3);
    c.close();
  });

  it('a successful open resets the backoff', async () => {
    vi.useFakeTimers();
    const c = new GatewayClient();
    c.connect();
    FakeSocket.last!.open();
    FakeSocket.last!.close();
    await vi.advanceTimersByTimeAsync(1001);
    FakeSocket.last!.open();          // stayed up
    FakeSocket.last!.close();
    await vi.advanceTimersByTimeAsync(1001);
    expect(FakeSocket.opened).toBe(3);
    c.close();
  });
});

describe('GatewayClient — events', () => {
  beforeEach(() => {
    FakeSocket.last = null;
    vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket);
    vi.stubGlobal('window', {
      ...globalThis.window,
      location: { protocol: 'https:', host: 'ava.local', href: '' },
      // Delegated lazily, NOT bound: `vi.useFakeTimers()` replaces the globals
      // after this stub is built, and a bound reference would keep calling the
      // real ones — so every reconnect test would sit waiting for wall-clock
      // time that the fake clock never advances.
      setTimeout: (...a: Parameters<typeof globalThis.setTimeout>) =>
        globalThis.setTimeout(...a),
      clearTimeout: (...a: Parameters<typeof globalThis.clearTimeout>) =>
        globalThis.clearTimeout(...a),
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it('routes an event to its topic and to the wildcard', () => {
    const c = new GatewayClient();
    c.connect();
    FakeSocket.last!.open();
    const step: unknown[] = [];
    const all: unknown[] = [];
    c.subscribe('run.step', (e) => step.push(e));
    c.subscribe('*', (e) => all.push(e));
    FakeSocket.last!.deliver({ op: 'event', topic: 'run.step', seq: 1, payload: { a: 1 } });
    FakeSocket.last!.deliver({ op: 'event', topic: 'other', seq: 2, payload: {} });
    expect(step).toHaveLength(1);
    expect(all).toHaveLength(2);
    c.close();
  });

  it('surfaces a gap as an event rather than hiding it', () => {
    // Consumers refetch. Papering over a gap renders a list with a hole in it
    // and tells the owner the record is complete.
    const c = new GatewayClient();
    c.connect();
    FakeSocket.last!.open();
    const gaps: unknown[] = [];
    c.subscribe('ava.gateway.gap', (e) => gaps.push(e));
    FakeSocket.last!.deliver({ op: 'gap', from: 4, to: 9 });
    FakeSocket.last!.deliver({ op: 'dropped', n: 12 });
    expect(gaps).toHaveLength(2);
    c.close();
  });

  it('resumes from the last seq it saw', async () => {
    vi.useFakeTimers();
    const c = new GatewayClient();
    c.connect();
    FakeSocket.last!.open();
    FakeSocket.last!.deliver({ op: 'event', topic: 't', seq: 42, payload: {} });
    FakeSocket.last!.close();
    await vi.advanceTimersByTimeAsync(1100);
    FakeSocket.last!.open();
    expect(FakeSocket.last!.frames()).toContainEqual({ op: 'resume', after: 42 });
    c.close();
    vi.useRealTimers();
  });

  it('unsubscribing removes the topic from the next subscribe frame', () => {
    const c = new GatewayClient();
    c.connect();
    FakeSocket.last!.open();
    const off = c.subscribe('run.step', () => {});
    off();
    const last = FakeSocket.last!.frames().filter((f) => f.op === 'subscribe').pop();
    expect(last.topics).not.toContain('run.step');
    c.close();
  });

  it('a malformed frame does not kill the socket', () => {
    const c = new GatewayClient();
    c.connect();
    FakeSocket.last!.open();
    const got: unknown[] = [];
    c.subscribe('t', (e) => got.push(e));
    FakeSocket.last!.onmessage?.({ data: 'not json' });
    FakeSocket.last!.deliver({ op: 'event', topic: 't', seq: 1, payload: {} });
    expect(got).toHaveLength(1);
    c.close();
  });
});

describe('GatewayClient — calls', () => {
  beforeEach(() => {
    vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket);
    vi.stubGlobal('window', {
      ...globalThis.window,
      location: { protocol: 'http:', host: 'x', href: '' },
      // Delegated lazily, NOT bound: `vi.useFakeTimers()` replaces the globals
      // after this stub is built, and a bound reference would keep calling the
      // real ones — so every reconnect test would sit waiting for wall-clock
      // time that the fake clock never advances.
      setTimeout: (...a: Parameters<typeof globalThis.setTimeout>) =>
        globalThis.setTimeout(...a),
      clearTimeout: (...a: Parameters<typeof globalThis.clearTimeout>) =>
        globalThis.clearTimeout(...a),
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  const respond = (body: unknown, status = 200) =>
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: status < 400, status, json: async () => body,
    })));

  it('sends requests over HTTP, never over the socket', async () => {
    // Split transport on purpose: a dropped socket must never lose a mutation.
    respond({ ok: true, payload: { sessions: [] } });
    const c = new GatewayClient();
    c.connect();
    FakeSocket.last!.open();
    await c.call('sessions.list');
    expect(FakeSocket.last!.frames().some((f) => f.method)).toBe(false);
    expect(fetch).toHaveBeenCalledWith('/api/gateway/rpc', expect.anything());
    c.close();
  });

  it('turns a coded 200 failure into a typed error', async () => {
    // `internal._told()`'s convention: a caller that cannot see the body cannot
    // show the owner the fix, so the failure rides in a 200.
    respond({ ok: false, error_code: 'agent_scope_denied', gw_code: 'FORBIDDEN',
      message: 'nope', detail: { missingScope: 'operator.admin' } });
    const c = new GatewayClient();
    await expect(c.call('chat.send')).rejects.toMatchObject({
      code: 'agent_scope_denied', gwCode: 'FORBIDDEN',
    });
  });

  it('carries an idempotency key when given one', async () => {
    const calls: [string, RequestInit][] = [];
    vi.stubGlobal('fetch', vi.fn(async (u: string, init: RequestInit) => {
      calls.push([u, init]);
      return { ok: true, status: 200, json: async () => ({ ok: true, payload: {} }) };
    }));
    const c = new GatewayClient();
    await c.call('chat.send', { text: 'hi' }, { idempotencyKey: 'turn:t1' });
    const body = JSON.parse(calls[0][1].body as string);
    expect(body.idempotency_key).toBe('turn:t1');
  });

  it('the error type carries what fixes.ts needs', () => {
    const e = new GatewayCallError({ error_code: 'agent_down', message: 'x' });
    expect(e.code).toBe('agent_down');
    expect(e).toBeInstanceOf(Error);
  });
});
