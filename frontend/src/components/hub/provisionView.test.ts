// The bar has ~40 meaningful state cells and the SPA has no component-render
// harness, so the decision lives in a pure function and gets covered here.
// Everything asserted below is a claim the UI must NOT make wrongly.
import { describe, expect, it } from 'vitest';
import { barView, currentStep, DRIFT_LABEL, mergeJob, summarize, tabPending } from './provisionView';
import type { ProvisionItem, ProvisionJob, ProvisionState } from './hubApi';

function state(over: Partial<ProvisionState> = {}): ProvisionState {
  const items = (over.items ?? []) as ProvisionItem[];
  const pending = items.filter((i) => i.state === 'stale' || i.state === 'undeployed').length;
  return {
    ok: true, state: 'deployed', runtime: 'nemoclaw', enabled: true, location: 'local',
    sandbox: { name: 'my-assistant', live: true, reason: '', rebuilt: false,
               model: 'nvidia/Nemotron', versions: {} },
    run: null,
    scopes: {
      persona: scope(items, 'persona'), policies: scope(items, 'policies'),
      servers: scope(items, 'servers'), skills: scope(items, 'skills'),
    },
    items,
    counts: { deployed: 0, stale: 0, undeployed: 0, unknown: 0, total: items.length },
    pending,
    scopes_to_provision: [...new Set(items
      .filter((i) => i.state === 'stale' || i.state === 'undeployed')
      .map((i) => i.scope))],
    ...over,
  } as ProvisionState;
}

function scope(items: ProvisionItem[], name: string) {
  const mine = items.filter((i) => i.scope === name);
  const pending = mine.filter((i) => i.state === 'stale' || i.state === 'undeployed').length;
  return {
    state: (pending ? 'stale' : 'deployed') as never,
    pending, source: 'registry',
    counts: { deployed: mine.length - pending, stale: pending, undeployed: 0,
              unknown: 0, total: mine.length },
  };
}

function item(sc: string, id: string, st: string, label?: string): ProvisionItem {
  return { scope: sc as never, id, label: label ?? id, state: st as never, source: 'registry' };
}

function job(over: Partial<ProvisionJob> = {}): ProvisionJob {
  return {
    status: 'idle', id: null, scope: null, started_at: null, ended_at: null,
    rc: null, steps: [], log: [], seq: 0, detail: '', observable: true, ...over,
  };
}

describe('barView', () => {
  it('shows nothing with no data — an absent capability is not a clean bill of health', () => {
    expect(barView(null, null).kind).toBe('hidden');
  });

  it('shows nothing when the owner turned the agent off', () => {
    const s = state({ enabled: false, items: [item('persona', 'IDENTITY.md', 'stale')] });
    expect(barView(s, null).kind).toBe('hidden');
  });

  it('shows nothing when everything is live', () => {
    expect(barView(state({ items: [item('skills', 'weather', 'deployed')] }), null).kind)
      .toBe('hidden');
  });

  it('never renders an all-clear bar (it would train blindness)', () => {
    const v = barView(state({ items: [item('skills', 'a', 'deployed')] }), null);
    expect(v.kind).not.toBe('pending');
    expect(v.kind).toBe('hidden');
  });

  it('counts stale and undeployed as pending', () => {
    const s = state({ items: [item('skills', 'a', 'stale'), item('policies', 'b', 'undeployed')] });
    const v = barView(s, null);
    expect(v.kind).toBe('pending');
    expect(v.kind === 'pending' && v.count).toBe(2);
  });

  it('NEVER counts unknown as pending — "we could not look" is not "it is missing"', () => {
    const s = state({ items: [item('skills', 'a', 'unknown'), item('persona', 'p', 'unknown')] });
    expect(barView(s, null).kind).toBe('hidden');
  });

  it('says the agent is down rather than offering a button that would fail', () => {
    const s = state({
      items: [item('skills', 'a', 'stale')],
      sandbox: { name: 'x', live: false, reason: 'the sandbox container is not running',
                 rebuilt: false, model: null, versions: {} },
    });
    const v = barView(s, null);
    expect(v.kind).toBe('agent-down');
    expect(v.kind === 'agent-down' && v.reason).toContain('not running');
  });

  it('reports a running job with its current step', () => {
    const j = job({ status: 'running', id: 'abc', started_at: 100,
                    steps: [{ scope: 'skills', id: 'weather', ok: true, detail: '' }] });
    const v = barView(state({ items: [item('skills', 'a', 'stale')] }), j);
    expect(v.kind).toBe('applying');
    expect(v.kind === 'applying' && v.step).toBe('Skills');
  });

  it('flags an unobservable run so the view does not draw a checklist that never fills', () => {
    const j = job({ status: 'running', id: 'abc', observable: false });
    const v = barView(state(), j);
    expect(v.kind === 'applying' && v.observable).toBe(false);
  });

  it('does not claim success when drift survives the run', () => {
    const s = state({ items: [item('policies', 'ava-email', 'undeployed')] });
    const v = barView(s, job({ status: 'done', id: 'abc' }));
    expect(v.kind).toBe('applied');
    expect(v.kind === 'applied' && v.partial).toBe(true);
  });

  it('reports a clean applied run', () => {
    const v = barView(state({ items: [item('skills', 'a', 'deployed')] }),
                      job({ status: 'done', id: 'abc' }));
    expect(v.kind === 'applied' && v.partial).toBe(false);
  });

  it('surfaces a failed run with its detail', () => {
    const v = barView(state(), job({ status: 'error', id: 'abc', detail: 'sandbox not found' }));
    expect(v.kind).toBe('failed');
    expect(v.kind === 'failed' && v.detail).toContain('sandbox not found');
  });

  it('a running job outranks everything else', () => {
    const s = state({ enabled: true, items: [item('skills', 'a', 'stale')],
                      sandbox: { name: 'x', live: false, reason: 'down', rebuilt: false,
                                 model: null, versions: {} } });
    expect(barView(s, job({ status: 'running', id: 'z' })).kind).toBe('applying');
  });
});

describe('summarize', () => {
  it('names items when the list is short', () => {
    const s = state({ items: [item('skills', 'weather', 'stale', 'Weather')] });
    expect(summarize(s)).toBe('Weather');
  });

  it('calls the persona "your persona", not by id', () => {
    const s = state({ items: [item('persona', 'IDENTITY.md', 'stale', 'Persona')] });
    expect(summarize(s)).toBe('your persona');
  });

  it('collapses to counts once the list gets long', () => {
    const items = Array.from({ length: 6 }, (_, i) => item('skills', `s${i}`, 'stale'));
    expect(summarize(state({ items }))).toBe('6 skills');
  });

  it('pluralises correctly', () => {
    const one = state({ items: [item('policies', 'a', 'stale'), item('policies', 'b', 'stale'),
                                item('policies', 'c', 'stale')] });
    expect(summarize(one)).toBe('3 app policies');
  });

  it('mixes persona and a collapsed count', () => {
    const items = [item('persona', 'IDENTITY.md', 'stale'),
      ...Array.from({ length: 4 }, (_, i) => item('skills', `s${i}`, 'stale'))];
    expect(summarize(state({ items }))).toBe('your persona, 4 skills');
  });

  it('is empty when nothing is pending', () => {
    expect(summarize(state({ items: [item('skills', 'a', 'deployed')] }))).toBe('');
  });

  it('ignores unknown items', () => {
    expect(summarize(state({ items: [item('skills', 'a', 'unknown')] }))).toBe('');
  });
});

describe('tabPending', () => {
  it('lights up only the tab that owns the change', () => {
    const s = state({ items: [item('persona', 'IDENTITY.md', 'stale')] });
    expect(tabPending(s)).toEqual({ persona: 1 });
  });

  it('routes skills to the agent tab', () => {
    expect(tabPending(state({ items: [item('skills', 'a', 'stale')] }))).toEqual({ agent: 1 });
  });

  it('sums policies and servers onto connectors', () => {
    const s = state({ items: [item('policies', 'a', 'stale'), item('servers', 'b', 'stale')] });
    expect(tabPending(s)).toEqual({ connectors: 2 });
  });

  it('is empty when the agent is disabled', () => {
    expect(tabPending(state({ enabled: false, items: [item('skills', 'a', 'stale')] }))).toEqual({});
  });

  it('is empty for a null state', () => {
    expect(tabPending(null)).toEqual({});
  });
});

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
    // The call to action lives in the bar, once — not repeated on every row.
    for (const label of Object.values(DRIFT_LABEL)) {
      expect(label).not.toMatch(/re-provision|click|press/i);
    }
  });

  it('does not over-claim on unknown', () => {
    expect(DRIFT_LABEL.unknown).toContain('unknown');
  });
});

describe('currentStep', () => {
  it('is null when nothing is running', () => {
    expect(currentStep(job({ status: 'done' }))).toBeNull();
    expect(currentStep(null)).toBeNull();
  });

  it('maps the scope to an owner-facing word', () => {
    const j = job({ status: 'running', steps: [{ scope: 'policies', id: 'x', ok: true, detail: '' }] });
    expect(currentStep(j)).toBe('App policies');
  });
});
