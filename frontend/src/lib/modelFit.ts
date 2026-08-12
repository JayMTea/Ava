// Whether a model will actually RUN well on this box, in one clause — and,
// more often, in no clause at all.
//
// The discipline here is as much about silence as about text. A green "fits!"
// beside every model is noise and a promise Ava cannot keep, so `should_fit`
// and `unknown` render NOTHING. Only the two readings an owner would act on
// ever speak, which is what stops this becoming a badge on every row.
//
// The spill case is the one this path exists for: a model that overflows the
// accelerator does not crash. It runs many times slower, and before this
// nothing anywhere in the app said so.
//
// Backend returns facts, the frontend words them (CLAUDE.md) — `verdict` is a
// machine token from ava_bridge/model_fit.py's FIT_VERDICTS and every sentence
// below is ours. Pure functions, because the SPA has no component-render
// harness and a decision left inside a component is one nobody can test.
import type { Tone } from '../components/hub/ui/Tile';

/** Closed vocabulary — mirrors FIT_VERDICTS in ava_bridge/model_fit.py.
 *  tests/test_fit_vocabulary.py fails if the two drift. */
export type FitVerdict = 'should_fit' | 'wont_fit' | 'will_spill' | 'unknown';

export type ModelFit = {
  verdict?: string;
  /** 'derived' = arithmetic on a size we read; 'observed' = we watched it run. */
  source?: string;
  detail?: string;
  need_gb?: number | null;
  pool_gb?: number | null;
  pool_kind?: string | null;
  /** Was this READ, or concluded? Same distinction hardware rows draw. */
  measured?: boolean;
  spilled?: boolean | null;
  headroom_gb?: number | null;
};

export type FitLine = { text: string; tone: Tone };

/** GB, without a fake decimal.
 *
 *  A pool the owner sees as "16 GB" must not be reported as "16.0 GB" — the
 *  extra digit reads as a measurement precise to 100 MB, which this is not. */
export function fitGb(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '';
  return Number.isInteger(v) ? `${v} GB` : `${v.toFixed(1)} GB`;
}

/** The one clause a model row says about memory, or null for silence.
 *
 *  null is the common case and the load-bearing one: `should_fit` says nothing
 *  because a model that fits needs no comment, and `unknown` says nothing
 *  because Ava could not read a size and will not guess at one out loud. */
export function fitLine(fit: ModelFit | null | undefined): FitLine | null {
  const v = fit?.verdict;

  if (v === 'wont_fit') {
    // Arithmetic, so it is stated flatly and shows its working. Both numbers,
    // because "too large" without them is an assertion the owner cannot check.
    const need = fitGb(fit?.need_gb);
    const pool = fitGb(fit?.pool_gb);
    const sums = need && pool ? ` — needs ${need}, this machine has ${pool}` : '';
    return { text: `Too large for this machine${sums}.`, tone: 'err' };
  }

  if (v === 'will_spill') {
    // The consequence, not the mechanism: "spills to system RAM" is a fact
    // about allocators, and "many times slower" is the thing the owner feels.
    const cost = 'which is many times slower';
    // Observed vs projected is the whole reason `measured` is on the wire. We
    // watched it happen, so it is reported in the past tense as a fact; a
    // projection is hedged, because it is one.
    return fit?.measured
      ? { text: `Ran partly on the processor last time, ${cost}.`, tone: 'warn' }
      : { text: `Likely to run partly on the processor, ${cost}.`, tone: 'warn' };
  }

  return null;   // should_fit, unknown, absent, or a token this build predates
}
