import { useCallback, useEffect, useState } from 'react';
import { agentApi, type AutomationJob } from '../../lib/agentApi';
import { useGateway } from '../../hooks/useGateway';
import { ago, EmptyState, Panel, StatCard } from '../ui/layout';
import { Badge } from '../hub/ui/Badge';

// What the agent does when you are not watching.
//
// Beside Activity rather than in Setup, because a cron job's RUN HISTORY is
// agent runs — the same thing Activity lists. Putting the schedule in
// configuration and its results here would split one object across two homes.

export function AutomationsList({ activeId, onOpen }: {
  activeId: string | null;
  onOpen: (id: string | null) => void;
}) {
  const client = useGateway();
  const [jobs, setJobs] = useState<AutomationJob[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!client) return;
    try {
      const got = await agentApi(client).automations.list();
      setJobs(got?.jobs || []);
      setError('');
    } catch (e) {
      setError((e as Error).message || String(e));
    } finally {
      setLoading(false);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  const active = jobs.filter((j) => j.enabled).length;
  const failing = jobs.filter((j) => j.lastStatus === 'failed').length;
  const selected = jobs.find((j) => j.id === activeId) || null;

  return (
    <>
      {/* Three tiles, so a three-track row — the default six leaves them
          bunched in the left half under a wide console. */}
      <div className="db-kpis db-kpis-3">
        <StatCard label="Automations" value={jobs.length} />
        <StatCard label="Active" value={active} tone={active ? 'ok' : 'default'} />
        {/* `err` only when something IS failing. A permanently red tile is a
            tile people stop reading. */}
        <StatCard label="Failing" value={failing} tone={failing ? 'err' : 'default'} />
      </div>

      <Panel title="Scheduled" subtitle="What runs on its own">
        {error ? (
          <p className="hub-msg err">{error}</p>
        ) : loading && !jobs.length ? (
          <p className="agent-list-note">Loading automations…</p>
        ) : !jobs.length ? (
          <EmptyState text="Nothing is scheduled. The agent only runs when you ask." />
        ) : (
          <ul className="agent-jobs">
            {jobs.map((j) => (
              <li key={j.id}>
                <button
                  type="button"
                  className={`agent-job${j.id === activeId ? ' on' : ''}`}
                  onClick={() => onOpen(j.id === activeId ? null : j.id)}
                  aria-expanded={j.id === activeId}
                >
                  <span className="agent-job-name">{j.name || j.id}</span>
                  <code className="agent-job-cron">{j.schedule || '—'}</code>
                  <Badge tone={
                    j.lastStatus === 'failed' ? 'err' : j.enabled ? 'ok' : 'muted'
                  }>
                    {j.enabled ? (j.lastStatus || 'idle') : 'paused'}
                  </Badge>
                  <span className="agent-job-when">
                    {j.lastRun ? ago(j.lastRun) : 'never run'}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      {selected && (
        <Panel
          title={selected.name || selected.id}
          subtitle="Run history"
          right={<button type="button" className="hub-btn ghost"
                         onClick={() => onOpen(null)}>Close</button>}
        >
          <div className="stat-rows">
            <div className="stat-row">
              <span className="stat-row-dot tone-muted" />
              <span>Schedule</span>
              <code>{selected.schedule || '—'}</code>
            </div>
          </div>
          {/* Deliberately not inventing a history the gateway has not been
              asked for. An empty panel that says why beats a fabricated one. */}
          <EmptyState text="Per-job run history arrives with the editor." />
        </Panel>
      )}
    </>
  );
}
