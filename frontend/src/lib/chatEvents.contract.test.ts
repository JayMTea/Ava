import { describe, expect, it } from 'vitest';
import { applyEvent, foldStep, startCot, type RunEvent, type RunContext } from './chatEvents';
import type { ChatItem } from './chatItems';
import type { CotStep } from './types';

// The OTHER half of qa/fakes/run-events.json.
//
// Ava's run-event vocabulary is spelled twice, in two languages, in files that
// never import each other: the runtime adapters emit it and this module folds
// it. Python proved the emit, vitest proved the parse, and nothing proved they
// AGREE — so a fifth kind, a renamed field or a dropped one would pass both
// suites and break a live turn, which is the one place nobody is watching.
//
// tests/test_run_event_vocabulary.py holds the emitter to the SAME file. It is
// imported rather than copied on purpose: two copies of a contract are two
// things that can disagree, which is the bug this exists to prevent.

import contract from '../../../qa/fakes/run-events.json';

const CONTRACT = contract as unknown as {
  kinds: string[];
  step_kinds: string[];
  samples: RunEvent[];
  required_fields: Record<string, string[]>;
  step_required_fields: Record<string, string[]>;
};

// The STEP vocabulary, pinned to the type. A `Record` over the union is checked
// by tsc in both directions — a kind missing from this object, or one CotStep
// does not know, is a build error — and the runtime comparison below ties the
// type to the contract file. This is the half the Python guard cannot see: it
// proves the backend's step kinds are ones this client actually types, not
// merely ones it fails to crash on.
const STEP_KINDS: Record<CotStep['kind'], true> = {
  thinking: true, text: true, tool: true, tool_result: true,
};

type StepEvent = Extract<RunEvent, { kind: 'step' }>;
const stepSamples = (): StepEvent[] =>
  CONTRACT.samples.filter((s): s is StepEvent => s.kind === 'step');

function ctx(cotId: string): RunContext {
  return { cotId, srcText: 'hello', srcAtts: [] };
}

function seed(): { items: ChatItem[]; id: string } {
  const cot = startCot(false);
  return { items: [cot], id: cot.id };
}

describe('the run-event contract, as the backend writes it', () => {
  it('is loaded and substantial (this test is worthless against an empty file)', () => {
    expect(CONTRACT.kinds.length).toBeGreaterThanOrEqual(4);
    expect(CONTRACT.samples.length).toBeGreaterThanOrEqual(4);
  });

  it('declares no kind this module silently treats as a final', () => {
    // `applyEvent` handles step / gap / error explicitly and lets EVERYTHING
    // ELSE fall through to the final branch — by design, but it means an
    // unrecognised kind does not do nothing, it ENDS THE TURN. So "did the
    // items change" cannot tell handled from unhandled (the first version of
    // this test asked exactly that, and passed while a bogus kind sailed
    // through). Compare against a kind that is definitely nonsense instead:
    // anything behaving identically to it is not being handled.
    const unhandled: string[] = [];
    for (const kind of CONTRACT.kinds) {
      const sample = CONTRACT.samples.find((s) => s.kind === kind);
      expect(sample, `no sample for kind '${kind}'`).toBeDefined();
      // BOTH results must come from the SAME seed. `seed()` mints a fresh cot
      // id, so comparing across two seeds compares two different ids and can
      // never match — which is how the previous version of this check passed
      // while a bogus kind sailed straight through it.
      const { items, id } = seed();
      const real = JSON.stringify(applyEvent(items, sample as RunEvent, ctx(id)));
      const fallback = JSON.stringify(
        applyEvent(items, { kind: '__not_a_kind__' } as never, ctx(id)));
      // `final` IS the fallback branch, so it is expected to match.
      if (kind !== 'final' && real === fallback) unhandled.push(kind);
    }
    expect(unhandled, 'the backend emits these and this module does not fold '
      + 'them — they fall through to the final branch and silently end the '
      + 'turn').toEqual([]);
  });

  it('folds every sample without throwing', () => {
    for (const sample of CONTRACT.samples) {
      const { items, id } = seed();
      expect(() => applyEvent(items, sample, ctx(id)),
        `kind '${sample.kind}' threw`).not.toThrow();
    }
  });

  it('a final carries its text through to the transcript', () => {
    const sample = CONTRACT.samples.find((s) => s.kind === 'final');
    const { items, id } = seed();
    const after = applyEvent(items, sample as RunEvent, ctx(id));
    expect(JSON.stringify(after)).toContain(
      (sample as { text: string }).text);
  });

  it('an error carries its message through', () => {
    const sample = CONTRACT.samples.find((s) => s.kind === 'error');
    const { items, id } = seed();
    const after = applyEvent(items, sample as RunEvent, ctx(id));
    expect(JSON.stringify(after)).toContain(
      (sample as { message: string }).message);
  });

  it('a gap says so rather than leaving a silent hole', () => {
    const { items, id } = seed();
    const after = applyEvent(items, { kind: 'gap' }, ctx(id));
    // The owner is entitled to know the record is incomplete.
    expect(JSON.stringify(after).toLowerCase()).toContain('not received');
  });
});

describe('the step-kind contract, as the backend writes it', () => {
  it('lists exactly the kinds CotStep types', () => {
    expect([...CONTRACT.step_kinds].sort())
      .toEqual(Object.keys(STEP_KINDS).sort());
  });

  it('never lists a step kind as a run kind', () => {
    // applyEvent dispatches on the OUTER kind; a step kind there would be
    // treated as a final and end the turn.
    for (const k of CONTRACT.step_kinds) expect(CONTRACT.kinds).not.toContain(k);
  });

  it('carries a step sample for every step kind, each with its required fields', () => {
    const sampled = new Set(stepSamples().map((s) => s.step.kind));
    expect([...sampled].sort()).toEqual([...CONTRACT.step_kinds].sort());
    for (const s of stepSamples()) {
      for (const field of CONTRACT.step_required_fields[s.step.kind] || []) {
        expect(s.step, `${s.step.kind} sample is missing ${field}`)
          .toHaveProperty(field);
      }
    }
  });

  it('folds every step sample, in contract order, into one tool card per call', () => {
    // The samples are written as one turn: a tool start then its result. Folding
    // them in order must merge the result INTO the start (by id), so what
    // renders is a single enriched `tool` step — never a duplicate row and never
    // a `tool_result` step left standing on its own.
    let steps: CotStep[] = [];
    for (const s of stepSamples()) {
      expect(() => { steps = foldStep(steps, s.step); },
        `step kind '${s.step.kind}' threw`).not.toThrow();
    }
    expect(steps.map((s) => s.kind)).not.toContain('tool_result');
    const tools = steps.filter((s) => s.kind === 'tool');
    expect(tools).toHaveLength(1);
    expect(tools[0].output).toBeTruthy();
  });

  it('every step sample reaches the transcript through applyEvent', () => {
    const { items, id } = seed();
    let next = items;
    for (const s of stepSamples()) next = applyEvent(next, s, ctx(id));
    const cot = next.find((it) => it.id === id);
    expect(cot && cot.kind === 'cot' ? cot.steps.length : 0).toBeGreaterThan(0);
  });
});
