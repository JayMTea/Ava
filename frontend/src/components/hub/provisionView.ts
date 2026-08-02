// Pure state-selection for the pending-changes bar.
//
// This file exists so the feature is testable. The SPA has no component-render
// harness (no jsdom, no @testing-library/react), so anything that lives inside a
// component is verifiable only through the headless-Chromium tier, which covers
// a handful of paths. The bar has ~40 meaningful state cells. Moving the decision
// into one pure function lets vitest cover all of them and leaves the component a
// logic-free `switch`.
//
// It also encodes the honesty rules, in one place:
//   * `unknown` is not pending. It means "we could not look inside the sandbox",
//     not "this is missing". Counting it inflates the number and makes Apply look
//     like it did less than it did.
//   * A finished job does not prove success. If drift survives the run, say so.
//   * With the agent switched off, say nothing at all — the owner chose that.
import type { DriftState, ProvisionJob, ProvisionScope, ProvisionState } from './hubApi';

export type BarView =
  | { kind: 'hidden' }
  | { kind: 'pending'; count: number; summary: string; scopes: ProvisionScope[] }
  | { kind: 'applying'; count: number; step: string | null; startedAt: number | null; observable: boolean }
  | { kind: 'applied'; summary: string; partial: boolean }
  | { kind: 'failed'; detail: string }
  | { kind: 'agent-down'; count: number; reason: string };

const SCOPE_NOUN: Record<ProvisionScope, [string, string]> = {
  persona: ['persona', 'persona'],
  policies: ['app policy', 'app policies'],
  servers: ['tool server', 'tool servers'],
  skills: ['skill', 'skills'],
};

export function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** Name the actual things when the list is short; collapse to counts when it is
 *  not. A bare number with no nouns tells the owner nothing they can act on. */
export function summarize(state: ProvisionState): string {
  const pendingItems = state.items.filter(
    (i) => i.state === 'stale' || i.state === 'undeployed',
  );
  if (!pendingItems.length) return '';

  const byScope = new Map<ProvisionScope, string[]>();
  for (const i of pendingItems) {
    const list = byScope.get(i.scope) ?? [];
    list.push(i.label || i.id);
    byScope.set(i.scope, list);
  }

  const nameable = pendingItems.length <= 3
    && [...byScope.values()].every((v) => v.length <= 2);

  const parts: string[] = [];
  for (const scope of ['persona', 'skills', 'policies', 'servers'] as ProvisionScope[]) {
    const list = byScope.get(scope);
    if (!list?.length) continue;
    if (scope === 'persona') {
      parts.push('your persona');
    } else if (nameable) {
      parts.push(...list);
    } else {
      const [one, many] = SCOPE_NOUN[scope];
      parts.push(plural(list.length, one, many));
    }
  }
  return parts.join(', ');
}

/** Which Hub tab owns each domain. A tab badge counts only what that tab owns,
 *  so one edit does not light up two tabs and read as two changes.
 *
 *  Persona moved INSIDE Agent, so it rolls up here rather than lighting a tab
 *  that no longer exists. The page-level count and the sub-tab breakdown must
 *  agree — a "3" on Agent that resolves to 1 + 1 once you open it is a number
 *  that lies. agentSubPending() is that breakdown, and the test asserts the sum. */
export function tabPending(state: ProvisionState | null): Record<string, number> {
  if (!state || !state.enabled) return {};
  const s = state.scopes;
  const out: Record<string, number> = {
    agent: (s?.persona?.pending ?? 0) + (s?.skills?.pending ?? 0),
    connectors: (s?.policies?.pending ?? 0) + (s?.servers?.pending ?? 0),
  };
  for (const k of Object.keys(out)) if (!out[k]) delete out[k];
  return out;
}

/** The same counts, split across Setup → Agent's own sub-tabs.
 *
 *  Policies and servers are absent on purpose — they belong to Connectors, not
 *  to a section of Agent. Runtime carries no badge either: the drift board on
 *  that sub-tab already lists every pending scope, so a badge beside it would
 *  count the same change twice. */
export function agentSubPending(state: ProvisionState | null): Record<string, number> {
  if (!state || !state.enabled) return {};
  const s = state.scopes;
  const out: Record<string, number> = {
    persona: s?.persona?.pending ?? 0,
    skills: s?.skills?.pending ?? 0,
  };
  for (const k of Object.keys(out)) if (!out[k]) delete out[k];
  return out;
}

/** The running step name, for the bar's one-line progress. */
export function currentStep(job: ProvisionJob | null): string | null {
  if (!job || job.status !== 'running') return null;
  const last = job.steps[job.steps.length - 1];
  if (!last) return null;
  const label: Record<string, string> = {
    persona: 'Persona', policies: 'App policies',
    servers: 'Tools', skills: 'Skills', runtime: 'Agent runtime',
  };
  return label[last.scope] ?? last.scope;
}

export function barView(state: ProvisionState | null, job: ProvisionJob | null): BarView {
  // No data at all — never loaded, or the bridge predates this endpoint. An
  // absent capability is not a clean bill of health, so show nothing.
  if (!state) return { kind: 'hidden' };

  // The owner explicitly turned the agent off. Rendering drift second-guesses a
  // decision the Agent panel already explains at length.
  if (!state.enabled) return { kind: 'hidden' };

  if (job?.status === 'running') {
    return {
      kind: 'applying',
      count: state.pending,
      step: currentStep(job),
      startedAt: job.started_at,
      observable: job.observable !== false,
    };
  }

  if (job?.status === 'error') {
    return { kind: 'failed', detail: job.detail || 'the run did not finish' };
  }

  if (job?.status === 'done') {
    // Do not trust "done" over the drift report. If something still has not
    // landed, the honest headline is partial success, not a green tick.
    if (state.pending > 0) {
      return { kind: 'applied', summary: summarize(state), partial: true };
    }
    return { kind: 'applied', summary: '', partial: false };
  }

  if (state.pending === 0) return { kind: 'hidden' };

  // There ARE changes, but the sandbox is not running, so Apply would fail at
  // install.sh's bootstrap guard. Say that instead of offering a dead button.
  if (!state.sandbox.live) {
    return { kind: 'agent-down', count: state.pending, reason: state.sandbox.reason };
  }

  return {
    kind: 'pending',
    count: state.pending,
    summary: summarize(state),
    scopes: state.scopes_to_provision ?? [],
  };
}

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
