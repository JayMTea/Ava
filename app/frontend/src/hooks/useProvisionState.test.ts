import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { hub } from '../components/hub/hubApi';
import type { ProvisionJob, ProvisionState } from '../components/hub/hubApi';
import { __resetForTests, __snapshotForTests, attachToProvisionJob, markProvisionDirty,
  openDriftView, refreshProvisionState, startProvision } from './useProvisionState';

vi.mock('../components/hub/hubApi', () => ({ hub: {
  provisionState: vi.fn(), provisionJob: vi.fn(), agentProvision: vi.fn(),
} }));

const state = (pending = 0) => ({ pending, enabled: true }) as ProvisionState;
const job = (over: Partial<ProvisionJob> = {}): ProvisionJob => ({
  id: 'run', status: 'done', scope: 'all', started_at: 1, ended_at: 2,
  rc: 0, steps: [], log: [], seq: 0, detail: '', observable: true, ...over,
});

beforeEach(() => {
  vi.resetAllMocks();
  __resetForTests();
  vi.stubGlobal('window', { dispatchEvent: vi.fn() });
  vi.mocked(hub.provisionState).mockResolvedValue(state());
  vi.mocked(hub.provisionJob).mockResolvedValue(job());
});
afterEach(() => { vi.unstubAllGlobals(); });

describe('agent setup recovery', () => {
  it('bypasses cached drift for an explicit re-check', async () => {
    await openDriftView(true);
    expect(hub.provisionState).toHaveBeenCalledWith(true);
    expect(hub.provisionJob).toHaveBeenCalledWith(0);
  });

  it('keeps cache invalidation after a failed read', async () => {
    markProvisionDirty('persona');
    vi.mocked(hub.provisionState).mockRejectedValueOnce(new Error('offline'));
    await refreshProvisionState();
    await refreshProvisionState();
    expect(hub.provisionState).toHaveBeenNthCalledWith(2, true);
  });

  it('ignores a slow response older than the latest re-check', async () => {
    let finish!: (value: ProvisionState) => void;
    vi.mocked(hub.provisionState).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    const old = refreshProvisionState();
    await refreshProvisionState(true);
    finish(state(7));
    await old;
    expect(__snapshotForTests().state?.pending).toBe(0);
  });

  it('restores a completed failure and its log on page open', async () => {
    vi.mocked(hub.provisionJob).mockResolvedValue(job({ status: 'error', log: ['connection refused'] }));
    await openDriftView();
    expect(__snapshotForTests().job?.status).toBe('error');
    expect(__snapshotForTests().job?.log).toEqual(['connection refused']);
  });

  it('keeps job lookup errors visible when drift succeeds', async () => {
    vi.mocked(hub.provisionJob).mockRejectedValue(new Error('offline'));
    await openDriftView();
    await refreshProvisionState();
    expect(__snapshotForTests().jobError).toContain('offline');
  });

  it('recovers a job from another tab after a start conflict', async () => {
    vi.mocked(hub.agentProvision).mockRejectedValue(Object.assign(new Error('busy'), { code: 'provision_running' }));
    vi.mocked(hub.provisionJob).mockResolvedValue(job({ id: 'other-tab', log: ['full log'] }));
    expect(await startProvision()).toEqual({ ok: true });
    expect(__snapshotForTests().job?.id).toBe('other-tab');
    expect(hub.provisionJob).toHaveBeenCalledWith(0);
  });

  it('forces fresh drift after a run finishes', async () => {
    await attachToProvisionJob();
    expect(hub.provisionState).toHaveBeenCalledWith(true);
    expect(window.dispatchEvent).toHaveBeenCalled();
  });
});
