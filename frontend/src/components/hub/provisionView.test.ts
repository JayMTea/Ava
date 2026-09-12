// What is left of the bar's test file. The selectors it covered went with the
// pending-changes banner; these two suites did not, and mergeJob's in particular
// went from covering dead code to covering the accumulator that now assembles a
// live Apply's log (useProvisionState.runJobLoop).
import { describe, expect, it } from 'vitest';
import { DRIFT_LABEL, mergeJob } from './provisionView';
import type { ProvisionJob } from './hubApi';

function job(over: Partial<ProvisionJob> = {}): ProvisionJob {
  return {
    status: 'idle', id: null, scope: null, started_at: null, ended_at: null,
    rc: null, steps: [], log: [], seq: 0, detail: '', observable: true, ...over,
  };
}

describe('mergeJob', () => {
  it('replaces wholesale when the run id changes', () => {
    const a = job({ id: 'a', log: ['one'] });
    const b = job({ id: 'b', log: ['two'] });
    expect(mergeJob(a, b)?.log).toEqual(['two']);
  });

  it('appends log lines within the same run', () => {
    const a = job({ id: 'a', log: ['one'] });
    const b = job({ id: 'a', log: ['two'] });
    expect(mergeJob(a, b)?.log).toEqual(['one', 'two']);
  });

  it('keeps the previous job when the poll returns nothing', () => {
    const a = job({ id: 'a' });
    expect(mergeJob(a, null)).toBe(a);
  });

  it('bounds the merged log', () => {
    const a = job({ id: 'a', log: Array.from({ length: 500 }, (_, i) => `l${i}`) });
    const b = job({ id: 'a', log: ['tail'] });
    expect(mergeJob(a, b)!.log.length).toBeLessThanOrEqual(400);
    const merged = mergeJob(a, b)!.log;
    expect(merged[merged.length - 1]).toBe('tail');
  });
});

describe('drift vocabulary', () => {
  it('states a fact rather than issuing an instruction', () => {
    // The call to action lives in Setup → Agent → Runtime, once — never
    // repeated on every row that happens to be out of date.
    for (const label of Object.values(DRIFT_LABEL)) {
      expect(label).not.toMatch(/re-provision|click|press/i);
    }
  });

  it('does not over-claim on unknown', () => {
    expect(DRIFT_LABEL.unknown).toContain('unknown');
  });
});
