// The drift vocabulary, and the accumulator for a running Apply.
//
// This file used to be "pure state-selection for the pending-changes bar" — the
// bar is gone, and with it every selector that decided what that banner and the
// Setup tab pills should say. What is left is the part other surfaces still
// need, kept pure so vitest can cover it without a render harness (the SPA has
// no jsdom and no @testing-library/react).
//
// One honesty rule outlives the bar and still governs DriftBadge and DriftBoard:
// `unknown` is NOT pending. It means "we could not look inside the sandbox", not
// "this is missing" — counting it inflates the number and makes Apply look like
// it did less than it did.
import type { DriftState, ProvisionJob } from './hubApi';

/** Merge a poll response over the previous job, dropping frames for a run that
 *  is no longer current. Same overlay idiom as the ops dashboard. */
export function mergeJob(prev: ProvisionJob | null, next: ProvisionJob | null): ProvisionJob | null {
  if (!next) return prev;
  if (!prev || prev.id !== next.id) return next;
  return { ...next, log: [...prev.log, ...next.log].slice(-400) };
}

export const DRIFT_LABEL: Record<DriftState, string> = {
  deployed: 'live',
  stale: 'edited · not applied',
  undeployed: 'new · not applied',
  unknown: 'unknown until applied',
};

export const DRIFT_TONE: Record<DriftState, 'ok' | 'warn' | 'muted'> = {
  deployed: 'ok',
  stale: 'warn',
  undeployed: 'warn',
  unknown: 'muted',
};
