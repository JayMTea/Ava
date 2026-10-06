// What a model row says about memory — and, mostly, what it does not.
//
// The feature this guards exists because a model that overflows the accelerator
// does not crash: it runs many times slower, and nothing anywhere in the app
// said so. The failure mode of the FIX is the opposite one — a badge on every
// row — so silence is tested first and hardest.
import { describe, expect, it } from 'vitest';
import { fitGb, fitLine } from './modelFit';
import type { ModelFit } from './modelFit';

const fit = (over: Partial<ModelFit>): ModelFit => ({
  verdict: 'should_fit', source: 'derived', detail: '', need_gb: 9.7,
  pool_gb: 16, pool_kind: 'vram', measured: false, spilled: null,
  headroom_gb: 6.3, ...over,
});

describe('silence', () => {
  it('says nothing at all about a model that fits', () => {
    // THE load-bearing case. A green "fits!" beside every row is noise and a
    // promise Ava cannot keep.
    expect(fitLine(fit({ verdict: 'should_fit' }))).toBeNull();
  });

  it('says nothing about a model it could not size', () => {
    // `unknown` means we could not read a size, never "it is fine". Guessing
    // out loud is how a fit advisory costs someone a model that ran fine.
    expect(fitLine(fit({ verdict: 'unknown', need_gb: null, headroom_gb: null })))
      .toBeNull();
  });

  it('says nothing when the bridge sent no fit at all', () => {
    // An older bridge has no `fit` key. Absent must read as silence, not as a
    // crash and not as a verdict.
    expect(fitLine(undefined)).toBeNull();
    expect(fitLine(null)).toBeNull();
    expect(fitLine({})).toBeNull();
  });

  it('says nothing for a verdict this build predates', () => {
    expect(fitLine(fit({ verdict: 'needs_a_bigger_boat' }))).toBeNull();
  });
});

describe("won't fit", () => {
  const line = fitLine(fit({ verdict: 'wont_fit', need_gb: 57.8, headroom_gb: -41.8 }));

  it('states it plainly, and shows its working', () => {
    // Arithmetic, so it is not hedged — and it prints both numbers, because
    // "too large" without them is an assertion the owner cannot check.
    expect(line?.text).toContain('Too large for this machine');
    expect(line?.text).toContain('57.8 GB');
    expect(line?.text).toContain('16 GB');
  });

  it('reads as an error, not a warning', () => {
    expect(line?.tone).toBe('err');
  });

  it('still says the headline when a number is missing', () => {
    const partial = fitLine(fit({ verdict: 'wont_fit', need_gb: null }));
    expect(partial?.text).toBe('Too large for this machine.');
  });
});

describe('spill', () => {
  it('hedges a projection, because it is one', () => {
    const l = fitLine(fit({ verdict: 'will_spill', measured: false }));
    expect(l?.text).toContain('Likely to run partly on the processor');
    // Never stated as certain. "will run partly" is the phrasing this guards.
    expect(l?.text).not.toMatch(/will run partly/i);
  });

  it('states an observation in the past tense, as fact', () => {
    // We watched it happen. `measured` is on the wire for exactly this.
    const l = fitLine(fit({ verdict: 'will_spill', measured: true, spilled: true }));
    expect(l?.text).toContain('Ran partly on the processor last time');
    expect(l?.text).not.toMatch(/likely/i);
  });

  it('names the consequence the owner feels, either way', () => {
    // Not "spills to system RAM" — that is a fact about allocators. The thing
    // the owner experiences is that it got slow.
    for (const measured of [true, false]) {
      expect(fitLine(fit({ verdict: 'will_spill', measured }))?.text)
        .toContain('many times slower');
    }
  });

  it('reads as a warning, distinct from won’t-fit', () => {
    const spill = fitLine(fit({ verdict: 'will_spill' }));
    const wont = fitLine(fit({ verdict: 'wont_fit' }));
    expect(spill?.tone).toBe('warn');
    expect(spill?.tone).not.toBe(wont?.tone);
  });
});

describe('fitGb', () => {
  it('does not invent a decimal a whole number does not have', () => {
    // "16.0 GB" claims precision to 100 MB on a figure that is a pool size.
    expect(fitGb(16)).toBe('16 GB');
    expect(fitGb(57.8)).toBe('57.8 GB');
    expect(fitGb(22.44)).toBe('22.4 GB');
  });

  it('renders nothing for a number that is not one', () => {
    expect(fitGb(null)).toBe('');
    expect(fitGb(undefined)).toBe('');
    expect(fitGb(NaN)).toBe('');
  });
});
