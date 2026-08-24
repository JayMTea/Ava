import { api, sleep } from '../lib/api';
import { applyTurnRecord, startCot, turnLabel } from '../lib/chatEvents';
import type { ChatItem } from '../lib/chatItems';
import { uid } from '../lib/chatItems';
import type { Artifact, Attachment } from '../lib/types';

// The bridge turn path, extracted from useChat UNCHANGED.
//
// WHY IT STILL EXISTS
// -------------------
// `POST /api/chat-stream` + `GET /api/turn/<id>` is the Direct floor's ONLY
// transport, and the Direct floor is what a fresh install, an unprovisioned box
// and a `agent.runtime: direct` opt-out all run on. Deleting this when the
// gateway path landed would have made "Ava works before you provision anything"
// false — which is the promise the floor exists to keep.
//
// So it moved rather than changed. The body below is the same loop, with the
// things it used to close over passed in as `deps` instead. It later gave up
// one piece on purpose: the finished-turn mapping (record -> chat items) lives
// in `lib/chatEvents.applyTurnRecord`, because the streamed strategy applies
// the SAME record at its terminal and two hand-written copies of "what a
// finished turn looks like" is exactly how they drift. The loop itself is
// still the extraction `qa/e2e/chat-flow.spec.ts` covers.

export interface TurnDeps {
  push(it: ChatItem): void;
  patch(id: string, fn: (it: ChatItem) => ChatItem): void;
  remove(id: string): void;
  setRealCtx(n: number): void;
  setArtifact(a: Artifact | null): void;
  /** The rolling last-12 transcript the bridge path sends as a form field. */
  history(): { role: string; content: string }[];
  pushHistory(role: string, content: string): void;
  /** Whole-list access, for `applyTurnRecord` — the shared finished-turn
   *  mapping appends items as well as patching the cot, and push/patch cannot
   *  express "fold the record onto the list" atomically. */
  setItems(fn: (xs: ChatItem[]) => ChatItem[]): void;
}

/** How long to wait before giving up on a turn. */
export const DEADLINE_MS = 200_000;

export async function runPolledTurn(
  t: string,
  atts: Attachment[],
  cid: string,
  userItemId: string | null,
  deps: TurnDeps,
): Promise<void> {
  const { push, patch, remove } = deps;
  // startCot rather than a hand-rolled item: applyTurnRecord derives `secs`
  // from `startedAt`, and the labels are the same strings turnLabel(0, …)
  // returns.
  const cot = startCot(atts.length > 0);
  const cotId = cot.id;
  push(cot);
  const ctx = { cotId, srcText: t, srcAtts: atts };
  const t0 = Date.now();
  const failUser = () => {
    if (userItemId) patch(userItemId, (it) => (it.kind === 'user' ? { ...it, failed: true } : it));
  };
  try {
    const fd = new FormData();
    fd.append('text', t);
    fd.append('history', JSON.stringify(deps.history().slice(0, -1)));
    fd.append('attachments', JSON.stringify(atts.map((a) => a.id)));
    fd.append('chat_id', cid);
    const start = await api.startTurn(fd);
    if (!start.turn_id) {
      remove(cotId);
      push({ kind: 'sys', id: uid(), text: start.error || 'could not start', icon: 'alert', code: start.error_code });
      failUser();
      return;
    }
    // Bound the poll loop so a stuck turn can never spin forever (the old
    // `for(;;)` would leave "Ava is thinking" up indefinitely on a hang, which
    // read as "no response"). Show honest elapsed-time hints while we wait,
    // and after DEADLINE_MS give up cleanly with a retryable error.
    let pollFails = 0;
    for (;;) {
      await sleep(750);
      const waited = Date.now() - t0;
      if (waited > DEADLINE_MS) {
        patch(cotId, (it) =>
          it.kind === 'cot'
            ? { ...it, status: 'error', error: 'Ava took too long to respond — please try again.' }
            : it,
        );
        failUser();
        return;
      }
      let s;
      try {
        s = await api.turn(start.turn_id);
        pollFails = 0;
      } catch {
        // Tolerate transient network blips, but don't poll a dead server forever.
        if (++pollFails > 20) {
          patch(cotId, (it) => (it.kind === 'cot' ? { ...it, status: 'error', error: 'lost connection to Ava' } : it));
          failUser();
          return;
        }
        continue;
      }
      if (s.steps) patch(cotId, (it) => (it.kind === 'cot' ? { ...it, steps: s.steps! } : it));
      // Keep the user informed while a slow turn is still working.
      if (s.status === 'running') {
        patch(cotId, (it) => (it.kind === 'cot'
          ? { ...it, label: turnLabel(waited, atts.length > 0) } : it));
      }
      if (s.status === 'done') {
        // The record -> items mapping is applyTurnRecord (lib/chatEvents.ts),
        // shared with the streamed strategy; the hook-level effects stay here.
        deps.setItems((xs) => applyTurnRecord(xs, s, ctx));
        if (typeof s.ctx_tokens === 'number') deps.setRealCtx(s.ctx_tokens);
        if (s.reply) deps.pushHistory('assistant', s.reply);
        if (s.artifact) deps.setArtifact(s.artifact);
        return;
      }
      if (s.status === 'error' || (!s.status && s.error)) {
        deps.setItems((xs) => applyTurnRecord(xs, s, ctx));
        // The banner polls slowly; a failure here is proof something is
        // wrong NOW, so let it re-check rather than disagreeing with the
        // error the user is looking at for up to 20 seconds.
        window.dispatchEvent(new Event('ava:turn-failed'));
        failUser();
        return;
      }
    }
  } catch {
    patch(cotId, (it) => (it.kind === 'cot' ? { ...it, status: 'error', error: 'network error' } : it));
    failUser();
  }
}
