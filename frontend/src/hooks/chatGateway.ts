import { api, sleep } from '../lib/api';
import type { GatewayClient } from '../lib/gatewayClient';
import {
  applyEvent, applyTurnRecord, markDisconnected, startCot, turnLabel,
  type RunContext, type RunEvent,
} from '../lib/chatEvents';
import type { ChatItem } from '../lib/chatItems';
import { uid } from '../lib/chatItems';
import type { Artifact, Attachment } from '../lib/types';
import { DEADLINE_MS } from './chatDirect';

// The streamed turn: submit through the BRIDGE, stream from the gateway.
//
// This file used to call `chat.send` on the gateway directly, which was wrong
// twice over. The params it sent (`{sessionId, text}`) are ones the live schema
// refuses outright — and even fixed, a direct send bypasses the one turn
// pipeline (`ava_bridge/turns.py`): no credentials note, no memory recall, no
// chats.db history (the drawer filled with dead "New chat" rows), no audit
// record, and a session key that differs from the voice path, splitting one
// conversation across two gateway sessions. Ghost mode's discard missed those
// sessions for the same reason.
//
// So every send now goes through POST /api/chat-stream, exactly like the
// polled Direct floor (`hooks/chatDirect.ts`) — same form fields, same
// `{turn_id}` answer. What stays streamed is the UX: this path watches the
// bridge-relayed `ava.run` topic instead of polling, so steps land live.
//
// Correlation is deterministic: `turns.py` starts every bridge run with
// idempotency key `turn:<turn_id>` and the gateway ADOPTS that as the runId
// (verified live against OpenClaw 2026.7.1). The runId is therefore known the
// moment the POST answers — no waiting for a response field, and no filtering
// by session in the meantime (chat events carry `sessionKey`, never a
// `sessionId` this client could have matched on; the old until-then session
// filter never matched anything).
//
// Everything that decides what the list looks like is in `lib/chatEvents.ts`
// and covered by vitest — including `applyTurnRecord`, the shared finished-
// turn mapping both strategies use. What is left here is the part that cannot
// be pure: subscribing, sending, ticking a clock, reconciling, cleaning up.

export interface StreamDeps {
  client: GatewayClient;
  setItems(fn: (xs: ChatItem[]) => ChatItem[]): void;
  setRealCtx(n: number): void;
  setArtifact(a: Artifact | null): void;
  /** The rolling last-12 transcript — the same form field chatDirect sends, so
   *  a mid-request fallback to the Direct floor still has context. */
  history(): { role: string; content: string }[];
  pushHistory(role: string, content: string): void;
  onDisconnect?(): void;
  /** The turn id, the moment the bridge issues it. Only the STREAMED path
   *  reports one: it is what makes a turn stoppable (the run announced itself,
   *  so there is something to abort), and the polled floor has no run to stop.
   *  A Stop button offered where nothing can be stopped is worse than none. */
  onTurnStarted?(tid: string): void;
}

/** How often the safety-net reconcile asks the record while a run is live. */
export const RECONCILE_MS = 5000;

export async function runStreamedTurn(
  t: string,
  atts: Attachment[],
  cid: string,
  userItemId: string | null,
  deps: StreamDeps,
): Promise<void> {
  const { client, setItems } = deps;
  const cot = startCot(atts.length > 0);
  const cotId = cot.id;
  const ctx: RunContext = { cotId, srcText: t, srcAtts: atts, runId: undefined };
  setItems((xs) => [...xs, cot]);

  const failUser = () => {
    if (!userItemId) return;
    setItems((xs) => xs.map((it) =>
      it.id === userItemId && it.kind === 'user' ? { ...it, failed: true } : it));
  };

  // The label is ticked LOCALLY, not by arrivals. A quiet run still has to say
  // "Still working…", and a label driven by events goes silent exactly when the
  // user most needs reassurance.
  const t0 = Date.now();
  const ticker = window.setInterval(() => {
    setItems((xs) => xs.map((it) =>
      it.id === cotId && it.kind === 'cot' && it.status === 'running'
        ? { ...it, label: turnLabel(Date.now() - t0, atts.length > 0) }
        : it));
  }, 1000);

  let settled = false;
  let resolveDone: () => void = () => {};
  const done = new Promise<void>((r) => { resolveDone = r; });
  const settle = () => { settled = true; resolveDone(); };

  let turnId = '';
  // The banner event and the history push each fire at most once, whichever of
  // the stream and the reconcile gets there first.
  let failAnnounced = false;
  const announceFail = () => {
    if (failAnnounced) return;
    failAnnounced = true;
    // The inference banner polls slowly; a failure here is proof something is
    // wrong NOW.
    window.dispatchEvent(new Event('ava:turn-failed'));
  };
  let historyPushed = false;

  // The authoritative record. Applying it is idempotent (see applyTurnRecord),
  // so it runs at the terminal AND on a ~5s safety net — which is what lets a
  // dropped socket still complete the turn. markDisconnected's promise
  // ("closing a laptop lid no longer loses a three-minute run") is only true
  // because of this poll; the events alone cannot keep it.
  const reconcile = async (): Promise<boolean> => {
    if (!turnId) return false;
    let s;
    try {
      s = await api.turn(turnId);
    } catch {
      return false; // transient — the next tick retries
    }
    const finished = s.status === 'done';
    const failed = s.status === 'error' || (!s.status && !!s.error);
    if (!finished && !failed) return false;
    setItems((xs) => applyTurnRecord(xs, s, ctx));
    if (typeof s.ctx_tokens === 'number') deps.setRealCtx(s.ctx_tokens);
    if (s.artifact) deps.setArtifact(s.artifact);
    if (finished) {
      if (s.reply && !historyPushed) {
        historyPushed = true;
        deps.pushHistory('assistant', s.reply);
      }
    } else {
      announceFail();
      failUser();
    }
    settle();
    return true;
  };

  // At the terminal event the record can lag the stream by a beat (this client
  // races the bridge's own persist), so try a few times before letting go
  // without the enrichment — the stream already painted text and tools, so
  // giving up costs the model chip, not the reply.
  const reconcileToSettle = async () => {
    for (let i = 0; i < 3; i++) {
      if (await reconcile()) return;
      if (settled) return;
      await sleep(700);
    }
    settle();
  };

  const onFrame = (p: RunEvent & { runId?: string }) => {
    // Filter by runId ONLY. The gateway broadcasts to every operator
    // connection, so an unfiltered reader would cheerfully finish this turn on
    // somebody else's reply.
    if (p.runId !== ctx.runId) return;
    setItems((xs) => applyEvent(xs, p as RunEvent, ctx));
    if (p.kind === 'final' || p.kind === 'error') {
      if (p.kind === 'error') {
        announceFail();
        failUser();
      }
      // The terminal frame carries text and tools; the model chip, artifact,
      // previews and the real ctx count exist only on the record.
      void reconcileToSettle();
    }
  };

  // Subscribe BEFORE sending. A short run can finish before the POST's
  // response is parsed — frames that arrive before the response names the turn
  // are held and replayed the moment it does.
  const pre: (RunEvent & { runId?: string })[] = [];
  const off = client.subscribe('ava.run', (ev) => {
    const p = (ev.payload || {}) as RunEvent & { runId?: string };
    if (!ctx.runId) {
      if (pre.length < 500) pre.push(p);
      return;
    }
    onFrame(p);
  });

  const offStatus = client.onStatus((phase) => {
    if (phase === 'down') {
      // NOT a failure. The run is very likely still executing, and the
      // reconcile poll above finishes it even if the socket never comes back.
      setItems((xs) => markDisconnected(xs, cotId));
      deps.onDisconnect?.();
    }
  });

  // An open socket is not proof a run is alive, so the watchdog stays.
  const watchdog = window.setTimeout(() => {
    setItems((xs) => applyEvent(xs, {
      kind: 'error',
      message: 'Ava took too long to respond — please try again.',
    }, ctx));
    failUser();
    settle();
  }, DEADLINE_MS);

  let net = 0;
  try {
    // The SAME ingress the Direct floor uses — the bridge's one turn pipeline.
    const fd = new FormData();
    fd.append('text', t);
    fd.append('history', JSON.stringify(deps.history().slice(0, -1)));
    fd.append('attachments', JSON.stringify(atts.map((a) => a.id)));
    fd.append('chat_id', cid);
    const start = await api.startTurn(fd);
    if (!start.turn_id) {
      setItems((xs) => [
        ...xs.filter((it) => it.id !== cotId),
        { kind: 'sys', id: uid(), text: start.error || 'could not start', icon: 'alert', code: start.error_code },
      ]);
      failUser();
      return;
    }
    turnId = start.turn_id;
    deps.onTurnStarted?.(turnId);
    // Deterministic — see the header. Assigned and flushed in one synchronous
    // block, so no frame can interleave between "we know the id" and "the
    // buffer has been judged against it".
    ctx.runId = `turn:${turnId}`;
    setItems((xs) => xs.map((it) =>
      it.id === cotId && it.kind === 'cot' ? { ...it, runId: ctx.runId } : it));
    pre.splice(0).forEach(onFrame);
    net = window.setInterval(() => { if (!settled) void reconcile(); }, RECONCILE_MS);
    await done;
  } catch (e) {
    const err = e as { message?: string; code?: string };
    setItems((xs) => applyEvent(xs, {
      kind: 'error',
      message: err.message || String(e),
      code: err.code,
    }, ctx));
    failUser();
  } finally {
    window.clearInterval(ticker);
    window.clearTimeout(watchdog);
    window.clearInterval(net);
    off();
    offStatus();
  }
}
