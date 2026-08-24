import { useCallback, useEffect, useState } from 'react';
import { agentApi, type AutomationJob } from '../../lib/agentApi';
import { useGateway } from '../../hooks/useGateway';
import { EmptyState } from '../dashboard/layout';
import { Badge } from '../hub/ui/Badge';

// What the agent is doing in the background for this session.
//
// Distinct from Automations, which is scheduled work with a cron expression.
// These are the in-flight tasks a run spawned, and they only mean anything
// while the session is open — which is why they are a PANEL and not a section.

export function TasksPanel({ sessionId }: { sessionId: string }) {
  const client = useGateway();
  const [tasks, setTasks] = useState<AutomationJob[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!client) return;
    try {
      // Scoped to THIS session. It listed every task in the process, which for
      // a panel headed "what this session is doing in the background" is a
      // different and wrong answer — and the giveaway was `sessionId` sitting
      // in the effect's deps while the fetch never used it.
      const got = await agentApi(client).tasks.list(sessionId);
      setTasks(got?.tasks || []);
      setError('');
    } catch (e) {
      setError((e as Error).message || String(e));
    } finally {
      setLoading(false);
    }
  }, [client, sessionId]);

  useEffect(() => { void load(); }, [load]);

  if (error) return <p className="hub-msg err">{error}</p>;
  if (loading && !tasks.length) return <p className="agent-list-note">Loading tasks…</p>;
  if (!tasks.length) return <EmptyState text="Nothing running in the background." />;

  return (
    <ul className="agent-tasks">
      {tasks.map((t) => (
        <li key={t.id} className="agent-task">
          <span className="agent-task-name">{t.name || t.id}</span>
          <Badge tone={t.lastStatus === 'failed' ? 'err' : t.enabled ? 'accent' : 'muted'}>
            {t.lastStatus || (t.enabled ? 'running' : 'paused')}
          </Badge>
        </li>
      ))}
    </ul>
  );
}
