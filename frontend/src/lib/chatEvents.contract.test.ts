import { describe, expect, it } from 'vitest';
import { applyEvent, startCot, type RunEvent, type RunContext } from './chatEvents';
import type { ChatItem } from './chatItems';

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
  samples: RunEvent[];
  required_fields: Record<string, string[]>;
};

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
