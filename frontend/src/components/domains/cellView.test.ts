/** Invented metric ids only — tracked source may not carry the owner's. */
import { describe, expect, it } from 'vitest';
import type { DomainCell, Observation } from '../../lib/types';
import {
  allGaps, fmtDay, HERO_SENTENCES, heroFor, nextAction, orderMetrics,
  ownerOfMetric, shownState, subtotalLine, unitLabel,
} from './cellView';

const obs = (o: Partial<Observation> = {}): Observation => ({
  metric: 'app-a.m1', unit: 'count', value: 1, state: 'ok',
  provenance: 'measured', n: null, ...o,
});

describe('the hero', () => {
  it('shows a figure only for a real single-valued reading', () => {
    expect(heroFor(obs()).kind).toBe('figure');
  });

  it('keeps a measured zero as a figure', () => {
    // A genuine zero IS a measurement. Treating every zero as suspect would be
    // its own dishonesty.
    const h = heroFor(obs({ value: 0 }));
    expect(h.kind).toBe('figure');
    expect(h.kind === 'figure' && h.obs.value).toBe(0);
  });

  it('never renders a figure for a dimensioned metric, even when state is ok', () => {
    // The backend reports ok if ANY dimension read while the cell-level value
    // stays null — an ok-first check would print a 48px em dash.
    const h = heroFor(obs({ value: null, by_dim: { d1: { value: 2, state: 'ok', provenance: 'measured', n: 1 } } }));
    expect(h.kind).toBe('sentence');
    expect(h.kind === 'sentence' && h.text).toBe(HERO_SENTENCES.dimensioned);
  });

  it('gives each absence its own sentence, and only a fault gets a warn tone', () => {
    const s = (o: Observation | null) => {
      const h = heroFor(o);
      return h.kind === 'sentence' ? h : null;
    };
    expect(s(obs({ state: 'insufficient', value: null }))?.text).toBe(HERO_SENTENCES.insufficient);
    expect(s(obs({ state: 'no_source', value: null }))?.text).toBe(HERO_SENTENCES.no_source);
    expect(s(obs({ state: 'unavailable', value: null }))?.tone).toBe('warn');
    // Absence must not look healthy AND must not look like an outage.
    expect(s(obs({ state: 'no_source', value: null }))?.tone).toBe('muted');
    expect(s(null)?.text).toBe(HERO_SENTENCES.none);
  });

  it('keeps all five sentences distinct', () => {
    expect(new Set(Object.values(HERO_SENTENCES)).size).toBe(5);
  });
});

describe('dimensioned metrics', () => {
  const dimObs = obs({
    value: null, state: 'ok', provenance: null,
    by_dim: {
      d1: { value: 1, state: 'ok', provenance: 'measured', n: 3 },
      d2: { value: null, state: 'unavailable', provenance: null, n: null, why: 'app down' },
    },
  });

  it('reports k-of-n rather than the backend ok', () => {
    const s = shownState(dimObs);
    expect(s.state).toBe('insufficient');
    expect(s.detail).toBe('1 of 2 read');
  });

  it('recovers a provenance the parent row reports as null', () => {
    // A false absence: two thirds measured, reported as no evidence at all.
    expect(shownState(dimObs).provenance).toBe('measured');
  });

  it('is ok only when every dimension read', () => {
    const all = obs({ value: null, by_dim: {
      d1: { value: 1, state: 'ok', provenance: 'measured', n: 1 },
      d2: { value: 2, state: 'ok', provenance: 'derived', n: 1 },
    } });
    const s = shownState(all);
    expect(s.state).toBe('ok');
    expect(s.provenance).toBe('derived');   // the weakest actually observed
  });

  it('surfaces each unread dimension in the missing list', () => {
    const cell = { gaps: [], metrics: [dimObs] } as unknown as DomainCell;
    const gaps = allGaps(cell);
    expect(gaps.map((g) => g.metric)).toContain('app-a.m1 [d2]');
    expect(gaps.find((g) => g.metric.endsWith('[d2]'))?.why).toBe('app down');
  });
});

describe('the table', () => {
  it('puts what needs attention first, stably', () => {
    const rows = [obs({ metric: 'a.1' }), obs({ metric: 'a.2', state: 'no_source', value: null }),
                  obs({ metric: 'a.3' }), obs({ metric: 'a.4', state: 'unavailable', value: null })];
    expect(orderMetrics(rows).map((o) => o.metric)).toEqual(['a.2', 'a.4', 'a.1', 'a.3']);
  });

  it('renders an absent day as an em dash, never undefined', () => {
    // A read-time ratio carries no `day` at all.
    expect(fmtDay(undefined)).toBe('—');
    expect(fmtDay(null)).toBe('—');
    expect(fmtDay('2026-01-01')).toBe('2026-01-01');
  });

  it('returns a unit token raw', () => {
    expect(unitLabel('usd_cents')).toBe('usd_cents');
    expect(unitLabel(null)).toBe('');
  });

  it('splits an owner off a metric id', () => {
    expect(ownerOfMetric('app-a.m1')).toBe('app-a');
    expect(ownerOfMetric('nodot')).toBe('');
  });
});

describe('next action', () => {
  it('offers the grant when one is pending for that metric', () => {
    const a = nextAction(obs({ state: 'no_source', value: null }),
      [{ connector: 'app-a', tool: 'get_x', tier: 'sensitive', metrics: ['app-a.m1'] }]);
    expect(a.kind).toBe('grant');
    expect(a.href).toBe('#hub/connectors');
  });

  it('offers no button for thin evidence', () => {
    // A button that does nothing is worse than none.
    const a = nextAction(obs({ state: 'insufficient', value: null }), []);
    expect(a.href).toBeUndefined();
    expect(a.text).toContain('days accumulate');
  });

  it('offers the app when a read failed', () => {
    expect(nextAction(obs({ state: 'unavailable', value: null }), []).kind).toBe('open');
  });
});

describe('subtotals', () => {
  it('names what it added, so a meaningless sum is self-evident', () => {
    const rows = [obs({ metric: 'a.intake', unit: 'g', value: 20 }),
                  obs({ metric: 'a.target', unit: 'g', value: 128 })];
    const line = subtotalLine(
      { unit: 'g', value: 148, contributors: 2, complete: true, missing: [] }, rows);
    expect(line.sums).toEqual(['a.intake', 'a.target']);
  });

  it('carries the exclusions when the sum is incomplete', () => {
    const line = subtotalLine(
      { unit: 'g', value: 20, contributors: 1, complete: false,
        missing: [{ metric: 'a.target', why: 'unavailable' }] }, []);
    expect(line.missing).toHaveLength(1);
  });

  it('renders a null subtotal as null, never zero', () => {
    const line = subtotalLine(
      { unit: 'g', value: null, contributors: 0, complete: false, missing: [] }, []);
    expect(line.value).toBeNull();
  });
});
