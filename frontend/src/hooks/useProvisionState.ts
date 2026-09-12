// One shared view of "what has my saved config actually reached Ava" — read when
// the owner asks for it, never on a clock.
//
// Drift is still a server fact. A pending set maintained here would lie after a
// reload, lie in a second tab, lie when the owner edits
// agent/skills/foo/SKILL.md on disk, and lie when somebody provisions from the
// CLI — so none is kept. What changed is the cadence: this module used to poll
// /provision/state every 10s for as long as Setup was open (30s elsewhere) and
// again on every tab focus, to keep a banner current that nobody had asked for.
// That is not a free read — computing drift can cost up to four `exec`
// round-trips into the sandbox (ava_bridge/provision.py). Now exactly one
// surface asks: Setup → Agent → Runtime, on mount and on Re-check.
//
// The job poll DOES still tick, and deliberately: it follows a run the owner
// started, for as long as that run lasts. That is a progress bar, not a watch.
//
// A module-scope store with a subscriber set rather than a Context provider:
// this codebase has no providers and the shell is prop-drilled. The job loop is
// shared, so the Runtime panel and a connector Deploy follow ONE run instead of
// polling the same job twice a second between them.
import { useEffect, useState } from 'react';
import { hub } from '../components/hub/hubApi';
import type { ProvisionJob, ProvisionScope, ProvisionState } from '../components/hub/hubApi';
import { mergeJob } from '../components/hub/provisionView';

type Snapshot = {
  state: ProvisionState | null;
  job: ProvisionJob | null;
  error: string;
  loading: boolean;
};

let snap: Snapshot = { state: null, job: null, error: '', loading: true };
const subs = new Set<() => void>();

// Domains the owner saved since the last drift read. This is NOT an optimistic
// pending set — it is one bit of routing: the next read must bypass the bridge's
// 30s drift cache, or a Save followed straight away by opening Runtime can be
// answered from a snapshot computed before the save.
const savedSinceLastRead = new Set<ProvisionScope>();

let jobLoop: Promise<void> | null = null;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function emit() {
  for (const fn of subs) fn();
}

function set(patch: Partial<Snapshot>) {
  snap = { ...snap, ...patch };
  emit();
}

/** Read drift from the bridge. The ONLY thing that computes it. */
export async function refreshProvisionState(): Promise<void> {
  const force = savedSinceLastRead.size > 0;
  savedSinceLastRead.clear();
  try {
    const state = await hub.provisionState(force);
    set({ state, error: '', loading: false });
  } catch (e) {
    // A failed fetch NEVER clears the last good snapshot: drift is sticky, so the
    // last known count is probably still true.
    set({ error: (e as Error).message, loading: false });
  }
}

/** "I just saved something the sandbox holds." Records nothing the UI reads — it
 *  only makes the next drift read bypass the server's cache. */
export function markProvisionDirty(scope: ProvisionScope): void {
  savedSinceLastRead.add(scope);
}

/** Everything the drift view needs, in one call: the current facts, plus any run
 *  already in flight (started in another tab, by a connector Deploy, or before an
 *  F5 — the job is server-side, so it is picked up mid-flight). */
export async function openDriftView(): Promise<void> {
  await refreshProvisionState();
  const j = await hub.provisionJob(0).catch(() => null);
  if (j?.status === 'running') {
    set({ job: j });
    void pollJob();
  }
}

/** Follow a provisioning run and resolve when it ends.
 *
 *  The connector Deploy button starts the same single-slot job rather than
 *  shelling `install.sh` a second time beside it, so it needs to wait on that run
 *  without owning it. It joins the shared loop below rather than opening a second
 *  one against the same job. */
export async function attachToProvisionJob(): Promise<ProvisionJob | null> {
  const first = await hub.provisionJob(0).catch(() => null);
  if (first) set({ job: first });
  await pollJob();
  return snap.job;
}

/** One loop per run, however many callers are watching. */
function pollJob(): Promise<void> {
  if (!jobLoop) jobLoop = runJobLoop().finally(() => { jobLoop = null; });
  return jobLoop;
}

async function runJobLoop(): Promise<void> {
  let misses = 0;
  for (;;) {
    const job = await hub.provisionJob(snap.job?.seq ?? 0).catch(() => null);
    if (!job) {
      // A dropped frame is not a finished run. Treating it as one used to pin the
      // view at "running" forever; with no background poll left to correct that,
      // it would hide the Apply button for the life of the page.
      if (++misses < 5) { await sleep(2000); continue; }
      set({
        job: null,
        error: 'Lost contact with Ava while applying. The run is still going on the '
             + 'bridge — reopen Setup → Agent → Runtime to see how it finished.',
      });
      return;
    }
    misses = 0;
    // Merge, never replace: the bridge slices the log by cursor, so each frame
    // carries only what is new. Replacing would leave "Show log" showing the last
    // second of install.sh and nothing before it.
    set({ job: mergeJob(snap.job, job) });
    if (job.status !== 'running') {
      await refreshProvisionState();
      // The sandbox model can change across a provision, and the header's model
      // chip is fetched once on mount. Broadcast so it can re-read — the existing
      // `ava:apps-changed` / `ava:theme` idiom, which keeps useChat from importing
      // hub code.
      window.dispatchEvent(new Event('ava:agent-provisioned'));
      return;
    }
    await sleep(1000);
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
    // Tier 3: a bridge that still answers synchronously. Nothing streams, but the
    // result is real — refresh and let the drift report speak.
    await refreshProvisionState();
    return { ok: !!r.ok, error: r.error };
  } catch (e) {
    // Someone else holds the single slot (a connector Deploy, the CLI, another
    // tab). Attach to their run rather than reporting a conflict: with one Apply
    // button left, "already running" is not something the owner can act on.
    if ((e as { code?: string }).code === 'provision_running') {
      void pollJob();
      return { ok: true };
    }
    return { ok: false, error: (e as Error).message };
  }
}

/** Subscribe to the shared snapshot.
 *
 *  `refreshOnMount` is what makes a surface an OWNER of drift rather than a reader
 *  of it, and exactly one surface passes it: Setup → Agent → Runtime. Every other
 *  place that mounted this hook was a reason to keep a timer alive. */
export function useProvisionState(opts?: { refreshOnMount?: boolean }) {
  const [, force] = useState(0);
  const refreshOnMount = !!opts?.refreshOnMount;
  useEffect(() => {
    const fn = () => force((n) => n + 1);
    subs.add(fn);
    if (refreshOnMount) void openDriftView();
    return () => { subs.delete(fn); };
  }, [refreshOnMount]);
  return {
    state: snap.state,
    job: snap.job,
    error: snap.error,
    loading: snap.loading,
    reload: refreshProvisionState,
  };
}

/** Test seam: reset module state between cases. */
export function __resetForTests() {
  snap = { state: null, job: null, error: '', loading: true };
  savedSinceLastRead.clear();
}
