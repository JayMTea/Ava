// The rules behind a destructive button. Pure, because the SPA has no
// component-render harness — same reason inferenceView.test.ts exists.
import { describe, expect, it } from 'vitest';

import { heldLabel, heldShort, receipt, rowOrder, rowView } from './allocView';
import type { AllocJob, AllocModel } from './hub/hubApi';

const model = (over: Partial<AllocModel> = {}): AllocModel => ({
  id: 'm', label: 'M', driver: 'http-unload', source: 'alloc', implicit: false,
  priority: 'batch', pinned: false, local: true, observe_only: false,
  weight_gb: 8, resident: true, resident_gib: 8, measured: true, ready: true,
  detail: '', modes: ['unload'], release_blocked: null, self_restoring: true,
  restore_kind: 'self', held_by: 0, released_by_owner: false, released_by_us: false,
  released_at: null, released_gib: null, is_brain: false, problems: [], notes: [],
  ...over,
});

const job = (over: Partial<AllocJob> = {}): AllocJob => ({
  status: 'done', model: 'm', verb: 'release', log: [],
  started_at: 1, ended_at: 2, result: { ok: true, code: 'ok' }, ...over,
});

describe('rowView — when a button is offered', () => {
  it('offers one for a model holding memory', () => {
    expect(rowView(model()).action?.verb).toBe('release');
  });

  it('still offers one when residency is UNKNOWABLE', () => {
    // The load-bearing case. `resident: null` means "we could not look", never
    // "no" — refusing the action would infer absence from ignorance, which is the
    // whole bug the residency vocabulary exists to prevent.
    const v = rowView(model({ resident: null, resident_gib: null, measured: false }));
    expect(v.action).not.toBeNull();
    expect(v.hint).toContain('safe to try');
  });

  it('offers nothing when the driver has no lever, and says so', () => {
    const v = rowView(model({ observe_only: true, modes: [] }));
    expect(v.action).toBeNull();
    expect(v.blocked).toBe('No release lever');
  });

  it('refuses while something is using it', () => {
    // Rule 4 of the planner, applied to a person: never take memory from work in
    // flight. There is deliberately no force option anywhere.
    const v = rowView(model({ held_by: 1 }));
    expect(v.action).toBeNull();
    expect(v.hint).toContain('work in progress');
  });

  it('refuses a pinned model outright', () => {
    expect(rowView(model({ pinned: true })).action).toBeNull();
  });

  it('refuses when the allocator has paused itself, and passes on the reason', () => {
    const v = rowView(model(), { quiesced: true, quiesce_reason: 'too many actions' });
    expect(v.action).toBeNull();
    expect(v.hint).toBe('too many actions');
  });

  it('never offers to free a model on another machine', () => {
    expect(rowView(model({ local: false })).action).toBeNull();
  });
});

describe('rowView — the verb matches the promise', () => {
  it('says "free" for an unload and "stop" when the process goes down', () => {
    // Conflating these would let "free memory" quietly mean "stop your container".
    expect(rowView(model({ modes: ['unload'] })).action?.label).toBe('Free its memory');
    expect(rowView(model({ modes: ['stop'] })).action?.label)
      .toBe('Stop it and free the memory');
  });

  it('confirms before stopping a process, but not for a cheap reversible unload', () => {
    // A confirm on every click trains dismissal.
    expect(rowView(model({ modes: ['unload'] })).confirm).toBe('');
    expect(rowView(model({ modes: ['stop'] })).confirm).not.toBe('');
  });

  it('warns differently for the brain depending on what it costs', () => {
    const self = rowView(model({ is_brain: true, self_restoring: true }));
    const hard = rowView(model({ is_brain: true, self_restoring: false,
                                 modes: ['stop'], restore_kind: 'start' }));
    expect(self.confirm).toContain('one slow reply');
    expect(hard.confirm).toContain('cannot answer chat');
    // Neither may imply data loss, because there is none.
    expect(hard.confirm).toContain('Nothing is lost');
  });

  it('flags a lever that has never been exercised', () => {
    const v = rowView(model({ notes: ['inferred — NOT VERIFIED against a live instance'] }));
    expect(v.hint).toContain('may turn out to do nothing');
  });
});

describe('rowView — after the owner has freed something', () => {
  it('offers a way back only when there is something to start', () => {
    const start = rowView(model({ released_by_owner: true, restore_kind: 'start',
                                  self_restoring: false }));
    expect(start.action?.verb).toBe('restore');
    expect(start.hint).toContain('will not start it again on its own');
  });

  it('offers no button for an engine that reloads itself', () => {
    // A button here would imply Ava is doing something it is not.
    const v = rowView(model({ released_by_owner: true, restore_kind: 'self' }));
    expect(v.action).toBeNull();
    expect(v.hint).toContain('reloads itself');
  });

  it('says who freed it in the VISIBLE slot, not only on hover', () => {
    // With no button and nothing blocked, the row would be a dot and a dash. The
    // size already says it holds nothing; only this says the owner is the reason.
    const v = rowView(model({ released_by_owner: true, restore_kind: 'self',
                              resident: false }));
    expect(v.blocked).toContain('You freed this');
    // A row that CAN be brought back says so with its button instead.
    expect(rowView(model({ released_by_owner: true, restore_kind: 'start',
                           self_restoring: false })).blocked).toBe('');
  });

  it('reads as a deliberate choice, never as a fault', () => {
    const v = rowView(model({ released_by_owner: true, resident: false }));
    expect(v.state).toBe('You freed this');
    expect(v.tone).not.toBe('err');
  });
});

describe('heldLabel — never quote config back as telemetry', () => {
  it('gives a figure only when it was measured', () => {
    expect(heldLabel(model({ resident_gib: 12.9, measured: true }))).toBe('12.9 GB');
    expect(heldLabel(model({ resident_gib: 12.9, measured: false })))
      .toBe('an unknown amount');
    expect(heldLabel(model({ resident: null, resident_gib: null, measured: false })))
      .toBe('an unknown amount');
  });

  it('says "nothing" only when it observed nothing', () => {
    expect(heldLabel(model({ resident: false }))).toBe('nothing');
  });
});

describe('heldShort — the same refusals, narrow enough for the column', () => {
  it('makes the same three distinctions as the long form', () => {
    // The compact form may not quietly become more confident than the sentence it
    // stands in for: an unmeasured size is still not a number, and an empty one is
    // still not "0 GB".
    expect(heldShort(model({ resident_gib: 12.9, measured: true }))).toBe('12.9 GB');
    expect(heldShort(model({ resident_gib: 12.9, measured: false }))).toBe('?');
    expect(heldShort(model({ resident: null, resident_gib: null, measured: false })))
      .toBe('?');
    expect(heldShort(model({ resident: false }))).toBe('—');
  });

  it('never renders an unmeasured figure', () => {
    expect(heldShort(model({ resident_gib: 35.0, measured: false })))
      .not.toContain('35');
  });
});

describe('receipt — degrade the claim, never invent a number', () => {
  it('shows nothing while there is nothing to report', () => {
    expect(receipt(job({ status: 'idle' }))).toBeNull();
    expect(receipt(job({ status: 'running' }))).toBeNull();
  });

  it('gives the full story when everything was measured', () => {
    const r = receipt(job({ result: { ok: true, code: 'ok', freed_gib: 12.4,
                                      free_before_gib: 3.1, free_after_gib: 15.5,
                                      measured: true } }));
    expect(r?.text).toContain('12.4 GB');
    expect(r?.text).toContain('3.1 GB free → 15.5 GB free');
  });

  it('says the pool did not move rather than claiming zero', () => {
    // "Freed 0 GB" states an outcome where there was none.
    const r = receipt(job({ result: { ok: true, code: 'ok', freed_gib: 0,
                                      measured: true } }));
    expect(r?.text).toContain('not moved yet');
    expect(r?.text).not.toContain('0.0 GB');
    expect(r?.ok).toBe(true);
  });

  it('admits when the box cannot measure at all, and still says what it knows', () => {
    // The WSL2 laptop: a card exists, no reader reaches it, the RAM figure never
    // moves. The release worked; the box cannot see it.
    const r = receipt(job({ result: { ok: true, code: 'ok', measured: false } }));
    expect(r?.text).toContain('no free-memory figure Ava trusts');
    expect(r?.text).toContain('did not refuse');
    expect(r?.ok).toBe(true);
  });

  it('never prints a number on a failure', () => {
    const r = receipt(job({ status: 'error',
                            result: { ok: false, code: 'release_declined',
                                      detail: 'HTTP 404' } }));
    expect(r?.ok).toBe(false);
    expect(r?.text).toContain('Nothing was done');
    expect(r?.text).not.toMatch(/\d+(\.\d+)? GB/);
  });

  it('explains a refusal in terms of what the owner should do next', () => {
    expect(receipt(job({ status: 'error', result: { ok: false, code: 'held_live' } }))
      ?.text).toContain('in use right now');
    expect(receipt(job({ status: 'error', result: { ok: false, code: 'turn_in_flight' } }))
      ?.text).toContain('answering right now');
  });
});

describe('rowOrder', () => {
  it('puts the brain first and shows every declared row', () => {
    // Rows with no lever are shown too: "Ava can see this and cannot free it" is
    // the honest answer on most stock installs, and hiding it reads as empty.
    const rows = rowOrder([
      model({ id: 'z', observe_only: true, modes: [] }),
      model({ id: 'brain', is_brain: true }),
      model({ id: 'a' }),
    ]);
    expect(rows.map((m) => m.id)).toEqual(['brain', 'a', 'z']);
    expect(rows).toHaveLength(3);
  });
});
