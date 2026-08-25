import type { ChatItem } from './chatItems';
import { uid } from './chatItems';
import type { CotStep, MediaRef, TurnStatus } from './types';

// The streamed turn, as a PURE reducer.
//
// Same rationale as `hubRoute.ts` and `provisionView.ts`: the SPA has no
// component-render harness, so a decision inside a component is checkable only
// through headless Chromium — far too coarse for "an event arrived twice" or
// "the final landed before the last step". Those are exactly the bugs a stream
// produces, so the decision lives here and vitest covers each one.
//
// Every function takes items and returns items. No fetch, no refs, no React.

/** What `AgentRuntime.iter_run` yields, as the client receives it. */
export type RunEvent =
  | { kind: 'step'; step: CotStep }
  | { kind: 'final'; text: string; tools?: string[]; attachments?: MediaRef[] }
  | { kind: 'error'; message: string; code?: string }
  | { kind: 'gap' };

/**
 * Add one step, folding a `tool_result` into its matching `tool` call — the
 * live-view twin of `turns._fold_step`. A tool call streams as a `tool` step
 * (start) then a `tool_result` (its output); merging the result INTO the start,
 * by id then by name, is what makes one enriched card per call instead of a
 * duplicate "Using exec" row. Immutable: returns a new array, never mutates.
 * An orphan result (its start was never seen) becomes a standalone tool card.
 */
export function foldStep(steps: CotStep[], step: CotStep): CotStep[] {
  if (step.kind === 'tool_result') {
    const idx = matchToolIndex(steps, step);
    if (idx >= 0) {
      const merged: CotStep = { ...steps[idx] };
      if (step.output) merged.output = step.output;
      if (step.attachments?.length) merged.attachments = step.attachments;
      if (step.is_error) merged.is_error = true;
      return steps.map((s, i) => (i === idx ? merged : s));
    }
    return [...steps, { ...step, kind: 'tool' }];
  }
  return [...steps, step];
}

function matchToolIndex(steps: CotStep[], result: CotStep): number {
  if (result.id) {
    for (let i = steps.length - 1; i >= 0; i--) {
      if (steps[i].kind === 'tool' && steps[i].id === result.id) return i;
    }
  }
  for (let i = steps.length - 1; i >= 0; i--) {
    const s = steps[i];
    if (s.kind === 'tool' && s.name === result.name
        && s.output === undefined && s.attachments === undefined) return i;
  }
  return -1;
}

export interface RunContext {
  /** The `cot` item this run is writing into. */
  cotId: string;
  /** What the user said, kept so the reply can offer Retry. */
  srcText: string;
  srcAtts: { id: string }[];
  runId?: string;
}

const GAP_TEXT = '(some steps were not received)';

function patch(items: ChatItem[], id: string,
               fn: (it: ChatItem) => ChatItem): ChatItem[] {
  return items.map((it) => (it.id === id ? fn(it) : it));
}

/**
 * Fold one event into the item list.
 *
 * The invariants worth stating, because each was a real decision:
 *
 *  * `secs` is set ONLY by `final`. See the note on the `cot` item — it is the
 *    live-vs-replay discriminator `ChainOfThought` reads.
 *  * A `final` after the run already finished is IGNORED rather than appended.
 *    A duplicate terminal event is a normal consequence of a reconnect that
 *    replays, and appending would show the reply twice.
 *  * A `gap` writes a visible step rather than being swallowed. Rendering a
 *    chain with a hole in it tells the owner the record is complete when it is
 *    not.
 *  * An event for a `cot` that is not there is dropped, not created. Late
 *    frames from a discarded run must not resurrect its UI.
 */
export function applyEvent(items: ChatItem[], ev: RunEvent,
                           ctx: RunContext): ChatItem[] {
  const cot = items.find((it) => it.id === ctx.cotId);
  if (!cot || cot.kind !== 'cot') return items;
  const settled = cot.status !== 'running';

  if (ev.kind === 'step') {
    if (settled) return items;
    return patch(items, ctx.cotId, (it) =>
      it.kind === 'cot' ? { ...it, steps: foldStep(it.steps, ev.step) } : it);
  }

  if (ev.kind === 'gap') {
    if (settled) return items;
    return patch(items, ctx.cotId, (it) =>
      it.kind === 'cot'
        ? { ...it, steps: [...it.steps, { kind: 'text', text: GAP_TEXT }] }
        : it);
  }

  if (ev.kind === 'error') {
    if (settled) return items;
    return patch(items, ctx.cotId, (it) =>
      it.kind === 'cot'
        ? { ...it, status: 'error', error: ev.message, code: ev.code }
        : it);
  }

  // final
  if (settled) return items;
  const secs = cot.startedAt
    ? Math.max(0, Math.round((Date.now() - cot.startedAt) / 1000))
    : 0;
  const next = patch(items, ctx.cotId, (it) =>
    it.kind === 'cot' ? { ...it, status: 'done', secs } : it);
  if (!ev.text && !(ev.attachments && ev.attachments.length)) return next;
  return [...next, {
    kind: 'ava',
    id: uid(),
    text: ev.text,
    toolsUsed: ev.tools || [],
    attachments: ev.attachments || [],
    srcText: ctx.srcText,
    srcAtts: ctx.srcAtts as never,
    runId: ctx.runId,
  }];
}

/**
 * Fold the AUTHORITATIVE turn record (`GET /api/turn/<id>`) onto the list.
 *
 * Both strategies end a turn here — the polled path because the record is its
 * only source, the streamed path as reconciliation: the stream's `final`
 * carries text and tools but not the model, the artifact, previews or the real
 * ctx count, and a dropped socket may have delivered nothing at all. The
 * streamed path also applies it on a ~5s safety net while running, so this is
 * written to be IDEMPOTENT:
 *
 *  * the reply is matched by runId. A bridge-path run's id is ALWAYS
 *    `turn:<turn_id>` — `ava_bridge/turns.py` starts every run with that as the
 *    idempotency key and the gateway adopts it (verified live) — so the record
 *    and the stream name the same run, and an existing bubble is ENRICHED
 *    rather than duplicated,
 *  * previews are tagged with the runId and appended once,
 *  * a record still `running` changes nothing — the live view belongs to the
 *    stream (or to the polled loop's own step patching),
 *  * a record whose cot is no longer in the list is dropped, same as
 *    `applyEvent`: a discarded run must not resurrect its UI.
 */
export function applyTurnRecord(items: ChatItem[], turn: TurnStatus,
                                ctx: RunContext): ChatItem[] {
  const finished = turn.status === 'done';
  const failed = turn.status === 'error' || (!turn.status && !!turn.error);
  if (!finished && !failed) return items;
  const cot = items.find((it) => it.id === ctx.cotId);
  if (!cot || cot.kind !== 'cot') return items;
  const runId = turn.id ? `turn:${turn.id}` : ctx.runId;

  if (failed) {
    // Same rule as applyEvent's settled guard: once a final landed, a later
    // failure frame must not un-finish the turn — two appliers over one cot
    // have to agree on that or they fight.
    if (cot.status === 'done') return items;
    return patch(items, ctx.cotId, (it) =>
      it.kind === 'cot'
        ? { ...it, status: 'error', error: turn.error || 'failed', code: turn.error_code }
        : it);
  }

  let next = patch(items, ctx.cotId, (it) => {
    if (it.kind !== 'cot') return it;
    const secs = typeof it.secs === 'number'
      ? it.secs
      : it.startedAt ? Math.max(0, Math.round((Date.now() - it.startedAt) / 1000)) : 0;
    return {
      ...it, status: 'done', secs,
      // The saved steps are the complete trajectory — adopting them heals the
      // "(some steps were not received)" hole a lossy stream had to admit to.
      steps: turn.steps && turn.steps.length ? turn.steps : it.steps,
    };
  });

  const existing = !!runId
    && next.some((it) => it.kind === 'ava' && it.runId === runId);
  if (existing) {
    next = next.map((it) =>
      it.kind === 'ava' && it.runId === runId
        ? {
            ...it,
            text: turn.reply ?? it.text,
            model: turn.model ?? it.model,
            toolsUsed: turn.tools_used ?? it.toolsUsed,
            artifact: turn.artifact ?? it.artifact ?? null,
            attachments: turn.attachments ?? it.attachments ?? [],
          }
        : it);
  } else if (turn.reply || (turn.attachments && turn.attachments.length)) {
    next = [...next, {
      kind: 'ava',
      id: uid(),
      text: turn.reply || '',
      model: turn.model ?? null,
      toolsUsed: turn.tools_used || [],
      artifact: turn.artifact ?? null,
      attachments: turn.attachments || [],
      srcText: ctx.srcText,
      srcAtts: ctx.srcAtts as never,
      runId,
    }];
  }

  if (turn.previews?.length
      && !(runId && next.some((it) => it.kind === 'preview' && it.runId === runId))) {
    next = [...next, ...turn.previews.map((p): ChatItem => (
      { kind: 'preview', id: uid(), preview: p, runId }))];
  }
  return next;
}

/**
 * The running head label.
 *
 * Ticked by a local interval rather than by events, because a quiet run still
 * has to say "Still working…" — a label driven by arrivals goes silent exactly
 * when the user most needs reassurance.
 */
export function turnLabel(elapsedMs: number, hasAtts: boolean): string {
  const secs = Math.round(elapsedMs / 1000);
  if (secs >= 45) return `Still working… (${secs}s)`;
  if (secs >= 20) return 'Working on it…';
  return hasAtts ? 'Ava is reading & thinking' : 'Ava is thinking';
}

/** A fresh `cot` item for a run that is about to start. */
export function startCot(hasAtts: boolean, runId?: string): ChatItem {
  return {
    kind: 'cot',
    id: uid(),
    label: turnLabel(0, hasAtts),
    steps: [],
    status: 'running',
    startedAt: Date.now(),
    runId,
  };
}

/**
 * Mark an in-flight run as disconnected — NOT as failed.
 *
 * A closed socket is not a failed run: it is very likely still executing on the
 * other side, and the reconnect reconciles against `chat.history`. Under the
 * polled path a 20-tick failure streak killed the turn, so closing a laptop lid
 * lost a three-minute run. This is the fix, and it only works if "the socket
 * went away" and "the agent failed" stay different states.
 */
export function markDisconnected(items: ChatItem[], cotId: string): ChatItem[] {
  return patch(items, cotId, (it) =>
    it.kind === 'cot' && it.status === 'running'
      ? { ...it, label: 'Lost the connection — reconnecting…' }
      : it);
}

/** A thread-shape marker (rewind / fork / branch switch). */
export function marker(kind: 'rewound' | 'forked' | 'branch-switch',
                       text: string, branchId?: string): ChatItem {
  return { kind: 'marker', id: uid(), marker: kind, text, at: Date.now(), branchId };
}
