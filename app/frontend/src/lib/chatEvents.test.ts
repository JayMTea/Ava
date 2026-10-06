import { afterEach, describe, expect, it, vi } from 'vitest';
import { applyEvent, applyTurnRecord, markDisconnected, marker, startCot, turnLabel } from './chatEvents';
import type { TurnStatus } from './types';
import type { ChatItem } from './chatItems';

const CTX = { cotId: '', srcText: 'hi', srcAtts: [] as { id: string }[] };

function fresh() {
  const cot = startCot(false, 'run-1') as Extract<ChatItem, { kind: 'cot' }>;
  return { items: [cot] as ChatItem[], ctx: { ...CTX, cotId: cot.id, runId: 'run-1' } };
}

const cotOf = (items: ChatItem[]) =>
  items.find((i) => i.kind === 'cot') as Extract<ChatItem, { kind: 'cot' }>;

afterEach(() => vi.useRealTimers());

describe('applyEvent — the happy sequence', () => {
  it('appends steps in arrival order', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'step', step: { kind: 'thinking', text: 'a' } }, ctx);
    items = applyEvent(items, { kind: 'step', step: { kind: 'tool', name: 'read' } }, ctx);
    expect(cotOf(items).steps.map((s) => s.kind)).toEqual(['thinking', 'tool']);
  });

  it('a final settles the chain and pushes the reply', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'final', text: 'Done.', tools: ['read'] }, ctx);
    expect(cotOf(items).status).toBe('done');
    const ava = items.find((i) => i.kind === 'ava') as Extract<ChatItem, { kind: 'ava' }>;
    expect(ava.text).toBe('Done.');
    expect(ava.toolsUsed).toEqual(['read']);
    expect(ava.runId).toBe('run-1');
  });

  it('a final with no text settles without pushing an empty bubble', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'final', text: '' }, ctx);
    expect(cotOf(items).status).toBe('done');
    expect(items.some((i) => i.kind === 'ava')).toBe(false);
  });

  it('the reply carries the source so Retry can re-ask', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'final', text: 'ok' }, ctx);
    const ava = items.find((i) => i.kind === 'ava') as Extract<ChatItem, { kind: 'ava' }>;
    expect(ava.srcText).toBe('hi');
  });
});

describe('the `secs` discriminator', () => {
  it('is NOT set while the run is in flight', () => {
    // ChainOfThought.tsx reads `typeof secs === 'number'` to tell a live chain
    // from a replayed one. Setting it early makes the two indistinguishable and
    // leaves the "Reasoning" wording unreachable.
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'step', step: { kind: 'text', text: 'x' } }, ctx);
    expect(cotOf(items).secs).toBeUndefined();
  });

  it('is written exactly once, at the final', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'final', text: 'a' }, ctx);
    const first = cotOf(items).secs;
    expect(typeof first).toBe('number');
    items = applyEvent(items, { kind: 'final', text: 'b' }, ctx);
    expect(cotOf(items).secs).toBe(first);
  });

  it('derives from startedAt, not from the event', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
    let { items, ctx } = fresh();
    vi.setSystemTime(new Date('2026-01-01T00:00:08Z'));
    items = applyEvent(items, { kind: 'final', text: 'x' }, ctx);
    expect(cotOf(items).secs).toBe(8);
  });
});

describe('applyEvent — the stream misbehaving', () => {
  it('a duplicate final does not render the reply twice', () => {
    // A reconnect that replays is normal, not exceptional.
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'final', text: 'Done.' }, ctx);
    items = applyEvent(items, { kind: 'final', text: 'Done.' }, ctx);
    expect(items.filter((i) => i.kind === 'ava')).toHaveLength(1);
  });

  it('an error AFTER a final does not un-finish the turn', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'final', text: 'Done.' }, ctx);
    items = applyEvent(items, { kind: 'error', message: 'late failure' }, ctx);
    expect(cotOf(items).status).toBe('done');
    expect(cotOf(items).error).toBeUndefined();
  });

  it('a step after a final is dropped rather than appended to a finished chain', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'final', text: 'Done.' }, ctx);
    items = applyEvent(items, { kind: 'step', step: { kind: 'text', text: 'late' } }, ctx);
    expect(cotOf(items).steps).toHaveLength(0);
  });

  it('an event for an unknown run is ignored, not materialised', () => {
    // Late frames from a discarded run must not resurrect its UI.
    const { items } = fresh();
    const out = applyEvent(items, { kind: 'final', text: 'x' },
      { ...CTX, cotId: 'nope' });
    expect(out).toBe(items);
  });

  it('a gap is written into the trajectory, not swallowed', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'step', step: { kind: 'text', text: 'a' } }, ctx);
    items = applyEvent(items, { kind: 'gap' }, ctx);
    const texts = cotOf(items).steps.map((s) => s.text);
    expect(texts).toContain('(some steps were not received)');
  });

  it('an error carries the code fixes.ts routes on', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'error', message: 'nope', code: 'agent_down' }, ctx);
    expect(cotOf(items).status).toBe('error');
    expect(cotOf(items).code).toBe('agent_down');
  });
});

describe('turnLabel', () => {
  it('escalates honestly as a run drags on', () => {
    expect(turnLabel(0, false)).toBe('Ava is thinking');
    expect(turnLabel(0, true)).toBe('Ava is reading & thinking');
    expect(turnLabel(25_000, false)).toBe('Working on it…');
    expect(turnLabel(50_000, false)).toBe('Still working… (50s)');
  });

  it('is a function of elapsed time only', () => {
    // Driven by a local interval, not by arrivals: a quiet run still has to
    // reassure, and a label that waits for an event goes silent exactly when
    // the user needs it most.
    expect(turnLabel(46_000, false)).toContain('46s');
  });
});

describe('markDisconnected', () => {
  it('a closed socket is not a failed run', () => {
    // The run is very likely still executing; the reconnect reconciles. Under
    // the polled path a failure streak killed the turn, so closing a laptop lid
    // lost a three-minute run.
    let { items, ctx } = fresh();
    items = markDisconnected(items, ctx.cotId);
    expect(cotOf(items).status).toBe('running');
    expect(cotOf(items).label).toContain('reconnecting');
  });

  it('leaves a finished run alone', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'final', text: 'x' }, ctx);
    const before = cotOf(items).label;
    items = markDisconnected(items, ctx.cotId);
    expect(cotOf(items).label).toBe(before);
  });
});

describe('markers', () => {
  it('all three thread-shape changes are one kind', () => {
    // Three kinds would be three renderers for one visual.
    for (const k of ['rewound', 'forked', 'branch-switch'] as const) {
      const m = marker(k, 'x');
      expect(m.kind).toBe('marker');
      if (m.kind === 'marker') expect(m.marker).toBe(k);
    }
  });

  it('carries a branch id when the change names one', () => {
    const m = marker('forked', 'forked to fix-auth', 'b-2');
    if (m.kind === 'marker') expect(m.branchId).toBe('b-2');
  });
});

describe('applyTurnRecord — the authoritative record', () => {
  // A bridge-path run's id is ALWAYS `turn:<turn_id>` (turns.py passes it as
  // the idempotency key the gateway adopts), so the record and the stream name
  // the same run without either being told about the other.
  const RUN = 'turn:t1';
  const record = (over: Partial<TurnStatus> = {}): TurnStatus => ({
    id: 't1',
    status: 'done',
    reply: 'Done.',
    tools_used: ['read'],
    model: { id: 'm1', label: 'Sandbox' },
    ...over,
  });
  const freshRun = () => {
    const cot = startCot(false, RUN) as Extract<ChatItem, { kind: 'cot' }>;
    return { items: [cot] as ChatItem[], ctx: { ...CTX, cotId: cot.id, runId: RUN } };
  };

  it('a record still running changes nothing — the stream owns the live view', () => {
    const { items, ctx } = freshRun();
    expect(applyTurnRecord(items, record({ status: 'running' }), ctx)).toBe(items);
  });

  it('settles a cot the stream never finished and appends the full reply', () => {
    // The dropped-socket case: no final ever arrived, the reconcile poll is
    // the only way this turn completes.
    let { items, ctx } = freshRun();
    items = applyTurnRecord(items, record({
      artifact: { type: 'weather' } as never,
      previews: [{ url: '/p/1' }],
      ctx_tokens: 1234,
    }), ctx);
    expect(cotOf(items).status).toBe('done');
    const ava = items.find((i) => i.kind === 'ava') as Extract<ChatItem, { kind: 'ava' }>;
    expect(ava.text).toBe('Done.');
    expect(ava.model?.id).toBe('m1');
    expect(ava.toolsUsed).toEqual(['read']);
    expect(ava.artifact).toEqual({ type: 'weather' });
    expect(ava.runId).toBe(RUN);
    expect(items.filter((i) => i.kind === 'preview')).toHaveLength(1);
  });

  it('correlates by runId: enriches the streamed bubble instead of duplicating it', () => {
    // The stream's final carries text and tools but no model/artifact — the
    // record fills those in ON the same bubble, matched by runId.
    let { items, ctx } = freshRun();
    items = applyEvent(items, { kind: 'final', text: 'Done.', tools: ['read'] }, ctx);
    items = applyTurnRecord(items, record({ artifact: { type: 'weather' } as never }), ctx);
    const avas = items.filter((i) => i.kind === 'ava') as Extract<ChatItem, { kind: 'ava' }>[];
    expect(avas).toHaveLength(1);
    expect(avas[0].model?.label).toBe('Sandbox');
    expect(avas[0].artifact).toEqual({ type: 'weather' });
  });

  it('a stream final AFTER the record has settled the cot is dropped too', () => {
    // The reverse race: the safety-net poll won, then the socket came back and
    // replayed the terminal frame.
    let { items, ctx } = freshRun();
    items = applyTurnRecord(items, record(), ctx);
    items = applyEvent(items, { kind: 'final', text: 'Done.' }, ctx);
    expect(items.filter((i) => i.kind === 'ava')).toHaveLength(1);
  });

  it('applying the same record twice is idempotent', () => {
    // The streamed path applies it at the terminal AND on a ~5s safety net.
    let { items, ctx } = freshRun();
    const rec = record({ previews: [{ url: '/p/1' }] });
    items = applyTurnRecord(items, rec, ctx);
    const again = applyTurnRecord(items, rec, ctx);
    expect(again.filter((i) => i.kind === 'ava')).toHaveLength(1);
    expect(again.filter((i) => i.kind === 'preview')).toHaveLength(1);
  });

  it('the saved steps replace a gapped stream trajectory', () => {
    let { items, ctx } = freshRun();
    items = applyEvent(items, { kind: 'step', step: { kind: 'text', text: 'a' } }, ctx);
    items = applyEvent(items, { kind: 'gap' }, ctx);
    items = applyTurnRecord(items, record({
      steps: [{ kind: 'text', text: 'a' }, { kind: 'tool', name: 'read' }],
    }), ctx);
    expect(cotOf(items).steps.map((st) => st.kind)).toEqual(['text', 'tool']);
  });

  it('a failed record carries the message AND the code fixes.ts routes on', () => {
    let { items, ctx } = freshRun();
    items = applyTurnRecord(items, record({
      status: 'error', reply: null, error: 'no model', error_code: 'model_unknown',
    }), ctx);
    expect(cotOf(items).status).toBe('error');
    expect(cotOf(items).error).toBe('no model');
    expect(cotOf(items).code).toBe('model_unknown');
  });

  it('a failure frame cannot un-finish a turn the record already completed', () => {
    let { items, ctx } = freshRun();
    items = applyTurnRecord(items, record(), ctx);
    items = applyTurnRecord(items, record({ status: 'error', error: 'late' }), ctx);
    expect(cotOf(items).status).toBe('done');
  });

  it('a record for a discarded run does not resurrect its UI', () => {
    const { items, ctx } = freshRun();
    const out = applyTurnRecord(items, record(), { ...ctx, cotId: 'nope' });
    expect(out).toBe(items);
  });
});

describe('foldStep — a tool result folds into its start', () => {
  it('a tool_result merges into the matching tool step by id → one card', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'step', step: { kind: 'tool', name: 'exec', id: 'c1', args: { cmd: 'render' } } }, ctx);
    items = applyEvent(items, { kind: 'step', step: { kind: 'tool_result', name: 'exec', id: 'c1', output: '601 frames', attachments: [{ url: '/uploads/x.mp4', kind: 'video' }] } }, ctx);
    const steps = cotOf(items).steps;
    expect(steps.map((s) => s.kind)).toEqual(['tool']);
    expect(steps[0].output).toBe('601 frames');
    expect(steps[0].args).toEqual({ cmd: 'render' });
    expect(steps[0].attachments?.[0].url).toBe('/uploads/x.mp4');
  });

  it('a result without an id folds by name into the un-resulted start', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'step', step: { kind: 'tool', name: 'exec' } }, ctx);
    items = applyEvent(items, { kind: 'step', step: { kind: 'tool_result', name: 'exec', output: 'done' } }, ctx);
    const steps = cotOf(items).steps;
    expect(steps.length).toBe(1);
    expect(steps[0].output).toBe('done');
  });

  it('an orphan result (no matching start) becomes a standalone tool card', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'step', step: { kind: 'tool_result', name: 'exec', output: 'surprise', is_error: true } }, ctx);
    const steps = cotOf(items).steps;
    expect(steps.map((s) => s.kind)).toEqual(['tool']);
    expect(steps[0].output).toBe('surprise');
    expect(steps[0].is_error).toBe(true);
  });

  it('folding is immutable — the prior array is not mutated', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'step', step: { kind: 'tool', name: 'exec', id: 'c1' } }, ctx);
    const before = cotOf(items).steps;
    items = applyEvent(items, { kind: 'step', step: { kind: 'tool_result', name: 'exec', id: 'c1', output: 'x' } }, ctx);
    expect(before[0].output).toBeUndefined();       // the old array untouched
    expect(cotOf(items).steps[0].output).toBe('x');  // the new one merged
  });
});

describe('attachments on the assistant message', () => {
  it('a final carries its media onto the reply bubble', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'final', text: 'Here.', attachments: [{ url: '/uploads/c.mp4', kind: 'video' }] }, ctx);
    const ava = items.find((i) => i.kind === 'ava') as Extract<ChatItem, { kind: 'ava' }>;
    expect(ava.attachments?.[0].url).toBe('/uploads/c.mp4');
  });

  it('a media-only final (no text) still pushes a bubble', () => {
    let { items, ctx } = fresh();
    items = applyEvent(items, { kind: 'final', text: '', attachments: [{ url: '/uploads/c.mp4', kind: 'video' }] }, ctx);
    expect(items.some((i) => i.kind === 'ava')).toBe(true);
  });

  it('applyTurnRecord enriches the streamed bubble with record media', () => {
    let { items, ctx } = (() => {
      const cot = startCot(false, 'turn:t1') as Extract<ChatItem, { kind: 'cot' }>;
      return { items: [cot] as ChatItem[], ctx: { ...CTX, cotId: cot.id, runId: 'turn:t1' } };
    })();
    items = applyEvent(items, { kind: 'final', text: 'Here.' }, ctx);
    items = applyTurnRecord(items, { id: 't1', status: 'done', reply: 'Here.', attachments: [{ url: '/uploads/c.mp4', kind: 'video' }] }, ctx);
    const avas = items.filter((i) => i.kind === 'ava');
    expect(avas.length).toBe(1);  // enriched, not duplicated
    expect((avas[0] as Extract<ChatItem, { kind: 'ava' }>).attachments?.[0].url).toBe('/uploads/c.mp4');
  });
});
