// One shared view of "what has my saved config actually reached Ava".
//
// There is deliberately NO client-owned pending set. Drift is a server fact: a
// set maintained here would lie after a reload, lie in a second tab, lie when the
// owner edits agent/skills/foo/SKILL.md on disk, and lie when somebody provisions
// from the CLI. So this is one polled resource plus a short-lived optimistic
// hint, shared by every consumer.
//
// A module-scope cache with a subscriber set rather than a Context provider: this
// codebase has no providers, the shell is prop-drilled, and the affordance has to
// be visible from both inside and outside #hub. This is the smallest thing that
// satisfies that, and its logic lives in pure exported functions so vitest can
// cover it without a render harness.
import { useEffect, useState } from 'react';
import { hub } from '../components/hub/hubApi';
import type { ProvisionJob, ProvisionScope, ProvisionState } from '../components/hub/hubApi';

type Snapshot = {
  state: ProvisionState | null;
  job: ProvisionJob | null;
  error: string;
  loading: boolean;
};

let snap: Snapshot = { state: null, job: null, error: '', loading: true };
const subs = new Set<() => void>();

// Domains the owner just saved, unioned into the server snapshot until the
// server itself agrees. Without this there is a window right after Save where
// the bar says "nothing pending" — the exact moment the feature is meant to
// speak. Same overlay-over-polled-snapshot idiom as the ops dashboard.
const dirtyHints = new Set<ProvisionScope>();
const cleanStreak = new Map<ProvisionScope, number>();

let timer = 0;
let pollingJob = false;

function emit() {
  for (const fn of subs) fn();
}

function set(patch: Partial<Snapshot>) {
  snap = { ...snap, ...patch };
  emit();
}

/** Union the optimistic hints into a server snapshot. Exported for test. */
export function applyDirtyHint(
  state: ProvisionState | null,
  hints: Set<ProvisionScope>,
): ProvisionState | null {
  if (!state || !hints.size) return state;
  const scopes = { ...state.scopes };
  let extra = 0;
  for (const scope of hints) {
    const cur = scopes[scope];
    if (!cur || cur.pending > 0) continue;   // server already agrees
    scopes[scope] = { ...cur, pending: 1, state: 'stale' };
    extra += 1;
  }
  if (!extra) return state;
  return {
    ...state,
    scopes,
    pending: state.pending + extra,
    scopes_to_provision: [...new Set([...state.scopes_to_provision, ...hints])],
  };
}

function reconcileHints(state: ProvisionState | null) {
  if (!state) return;
  for (const scope of [...dirtyHints]) {
    const pending = state.scopes?.[scope]?.pending ?? 0;
    if (pending > 0) {
      // The server sees it too; the hint has done its job.
      dirtyHints.delete(scope);
      cleanStreak.delete(scope);
      continue;
    }
    // Two consecutive clean reads means the save was a genuine no-op (re-saving
    // identical text), so stop claiming otherwise.
    const n = (cleanStreak.get(scope) ?? 0) + 1;
    cleanStreak.set(scope, n);
    if (n >= 2) {
      dirtyHints.delete(scope);
      cleanStreak.delete(scope);
    }
  }
}

export async function refreshProvisionState(): Promise<void> {
  try {
    const state = await hub.provisionState();
    reconcileHints(state);
    set({ state, error: '', loading: false });
  } catch (e) {
    // A failed fetch NEVER clears the last good snapshot: drift is sticky, so the
    // last known count is probably still true. Only claims about *now* get
    // suppressed, which barView() handles.
    set({ error: (e as Error).message, loading: false });
  }
}

export function markProvisionDirty(scope: ProvisionScope): void {
  dirtyHints.add(scope);
  cleanStreak.delete(scope);
  emit();
  void refreshProvisionState();
}

/** Follow a provisioning run that something ELSE started, and resolve when it
 *  ends.
 *
 *  The connector Deploy button starts the same single-slot job rather than
 *  shelling `install.sh` a second time beside it, so it needs to wait on that
 *  run without owning it. Everything the bar and the run view already render
 *  keeps working, because it is the same job. */
export async function attachToProvisionJob(): Promise<ProvisionJob | null> {
  const first = await hub.provisionJob(0).catch(() => null);
  if (first) set({ job: first });
  void pollJob();
  for (;;) {
    const job = await hub.provisionJob(snap.job?.seq ?? 0).catch(() => null);
    if (!job || job.status !== 'running') {
      await refreshProvisionState();
      return job;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

async function pollJob() {
  if (pollingJob) return;
  pollingJob = true;
  try {
    for (;;) {
      const job = await hub.provisionJob(snap.job?.seq ?? 0).catch(() => null);
      if (!job) break;
      set({ job });
      if (job.status !== 'running') {
        await refreshProvisionState();
        // The sandbox model can change across a provision, and the header's
        // model chip is fetched once on mount. Broadcast so it can re-read —
        // the existing `ava:apps-changed` / `ava:theme` idiom, which keeps
        // useChat from importing hub code.
        window.dispatchEvent(new Event('ava:agent-provisioned'));
        break;
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
  } finally {
    pollingJob = false;
  }
}

export async function startProvision(
  scope: ProvisionScope | 'all' = 'all',
): Promise<{ ok: boolean; error?: string }> {
  try {
    const r = await hub.agentProvision(scope);
    if (r.job_id) {
      set({ job: { ...(snap.job as ProvisionJob), status: 'running', id: r.job_id,
                   scope, started_at: Date.now() / 1000, ended_at: null, steps: [],
                   log: [], seq: 0, rc: null, detail: '', observable: true } });
      void pollJob();
      return { ok: true };
    }
    // Tier 3: a bridge that still answers synchronously. Nothing streams, but
    // the result is real — refresh and let the drift report speak.
    await refreshProvisionState();
    return { ok: !!r.ok, error: r.error };
  } catch (e) {
    return { ok: false, error: (e as Error).message };
  }
}

function schedule() {
  window.clearTimeout(timer);
  // Drift is sticky; 3s polling would be theatre. Suspended entirely while a job
  // runs — the job poll is the authority then, and refetching drift mid-run makes
  // the count flicker.
  const inHub = location.hash.startsWith('#hub');
  const ms = snap.job?.status === 'running' ? 30_000 : inHub ? 10_000 : 30_000;
  timer = window.setTimeout(async () => {
    if (!document.hidden && snap.job?.status !== 'running') await refreshProvisionState();
    schedule();
  }, ms);
}

function startPolling() {
  void refreshProvisionState();
  // A run may already be in flight from another tab or a page reload; the job is
  // server-side, so the view reappears mid-flight rather than being lost.
  void hub.provisionJob(0).then((j) => {
    if (j?.status === 'running') { set({ job: j }); void pollJob(); }
  }).catch(() => {});
  schedule();
  document.addEventListener('visibilitychange', onVis);
}

function onVis() {
  if (!document.hidden) void refreshProvisionState();
}

function stopPolling() {
  window.clearTimeout(timer);
  document.removeEventListener('visibilitychange', onVis);
}

export function useProvisionState() {
  const [, force] = useState(0);
  useEffect(() => {
    const fn = () => force((n) => n + 1);
    subs.add(fn);
    if (subs.size === 1) startPolling();
    return () => {
      subs.delete(fn);
      if (subs.size === 0) stopPolling();
    };
  }, []);
  return {
    state: applyDirtyHint(snap.state, dirtyHints),
    job: snap.job,
    error: snap.error,
    loading: snap.loading,
    reload: refreshProvisionState,
  };
}

/** Test seam: reset module state between cases. */
export function __resetForTests() {
  snap = { state: null, job: null, error: '', loading: true };
  dirtyHints.clear();
  cleanStreak.clear();
}

/** Test seam: drive the hint reconciliation without a network. */
export function __hints() {
  return dirtyHints;
}
export { reconcileHints as __reconcileHints };
