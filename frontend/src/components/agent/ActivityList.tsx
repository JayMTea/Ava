import { useCallback, useEffect, useMemo, useState } from 'react';
import { agentApi } from '../../lib/agentApi';
import { useGateway } from '../../hooks/useGateway';
import { ago, EmptyState, Panel } from '../ui/layout';
import { Badge } from '../hub/ui/Badge';
import { groupRunsByDay, runTone, type RunRow } from './agentView';

// What the agent DID — the past tense of Sessions.
//
// Grouped by day rather than paged, because the question this answers is "what
// happened yesterday", not "show me rows 40–60". The Run Inspector hangs off a
// row at `#agent/activity/run/<id>`, which is bookmarkable for the same reason
// a session is: it is a thing you send somebody.

const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'failed', label: 'Failed' },
  { id: 'running', label: 'Running' },
] as const;

type FilterId = (typeof FILTERS)[number]['id'];

export function ActivityList({ onOpenRun }: { onOpenRun: (id: string) => void }) {
  const client = useGateway();
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<FilterId>('all');
  const [q, setQ] = useState('');

  const load = useCallback(async () => {
    if (!client) return;
    try {
      const got = await agentApi(client).audit.activity();
      setRuns((got?.runs || []) as RunRow[]);
      setError('');
    } catch (e) {
      setError((e as Error).message || String(e));
    } finally {
      setLoading(false);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const rows = runs.filter((r) => {
      const st = String(r.status || '').toLowerCase();
      if (filter === 'failed' && !/fail|error/.test(st)) return false;
      if (filter === 'running' && st !== 'running') return false;
      if (!needle) return true;
      return `${r.title || ''} ${r.sessionId || ''} ${r.id}`.toLowerCase().includes(needle);
    });
    // `Date.now()` read here rather than captured: a page left open overnight
    // has to stop calling yesterday "Today".
    return groupRunsByDay(rows, Date.now());
  }, [runs, filter, q]);

  return (
    <Panel
      title="Activity"
      subtitle="What the agent did, grouped by day"
      right={
        <span className="agent-filters">
          <input
            className="agent-search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search runs"
            aria-label="Search runs"
          />
          {FILTERS.map((f) => (
            <button
              type="button"
              key={f.id}
              className={`hub-btn ghost${filter === f.id ? ' on' : ''}`}
              aria-pressed={filter === f.id}
              onClick={() => setFilter(f.id)}
            >
              {f.label}
            </button>
          ))}
        </span>
      }
    >
      {error ? (
        <p className="hub-msg err">{error}</p>
      ) : loading && !runs.length ? (
        <p className="agent-list-note">Loading activity…</p>
      ) : !groups.length ? (
        <EmptyState text={runs.length
          ? 'No runs match that filter.'
          : 'Nothing has run yet.'} />
      ) : (
        groups.map((g) => (
          <section key={g.key} className="hub-group">
            <h3 className="hub-group-title">{g.label}</h3>
            <ul className="agent-runs">
              {g.runs.map((r) => (
                <li key={r.id}>
                  <button type="button" className="agent-run"
                          onClick={() => onOpenRun(r.id)}>
                    <span className={`agent-row-dot tone-${runTone(r.status)}`}
                          aria-hidden="true" />
                    <span className="agent-run-title">
                      {r.title || r.sessionId || r.id}
                    </span>
                    <Badge tone={runTone(r.status)}>{r.status || 'unknown'}</Badge>
                    <span className="agent-run-when">{r.at ? ago(r.at) : ''}</span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ))
      )}
    </Panel>
  );
}
