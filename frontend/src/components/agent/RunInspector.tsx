import { useCallback, useEffect, useState } from 'react';
import { agentApi } from '../../lib/agentApi';
import { useGateway } from '../../hooks/useGateway';
import { EmptyState, Panel } from '../ui/layout';
import { Badge } from '../hub/ui/Badge';
import { StatRow } from '../hub/ui/StatRow';
import { receiptBadge, type Receipt } from './agentView';

// One run, and what it was allowed to do.
//
// The honest framing matters more than the layout here. This is BEST-EFFORT
// operational evidence, not a compliance archive — it never shows prompts,
// command bodies, arguments, paths or credentials — and the page says so rather
// than letting a reader assume they are looking at a complete record.

const RECEIPT_CAP = 50;

interface RunDetail {
  id?: string;
  trustDomain?: string;
  ingress?: string;
  invoker?: string;
  agent?: string;
  runtime?: string;
  receipts?: Receipt[];
  more?: boolean;
}

export function RunInspector({ runId, onBack }: {
  runId: string;
  onBack: () => void;
}) {
  const client = useGateway();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!client) return;
    setLoading(true);
    try {
      setRun((await agentApi(client).audit.run(runId)) as RunDetail);
      setError('');
    } catch (e) {
      setError((e as Error).message || String(e));
    } finally {
      setLoading(false);
    }
  }, [client, runId]);

  useEffect(() => { void load(); }, [load]);

  const receipts = (run?.receipts || []).slice(0, RECEIPT_CAP);

  return (
    <Panel
      title={`Run ${runId}`}
      subtitle="Where it came from, and what gated it"
      right={<button type="button" className="hub-btn ghost" onClick={onBack}>Back</button>}
    >
      {error && <p className="hub-msg err">{error}</p>}
      {loading && !run && <p className="agent-list-note">Loading the run…</p>}

      {run && (
        <>
          <div className="stat-rows">
            <StatRow label="Trust domain" value={run.trustDomain || '—'}
                     tone={run.trustDomain ? 'ok' : 'muted'} />
            <StatRow label="Ingress" value={run.ingress || '—'}
                     tone={run.ingress ? 'ok' : 'muted'} />
            <StatRow label="Invoked by" value={run.invoker || '—'}
                     tone={run.invoker ? 'ok' : 'muted'} />
            <StatRow label="Agent" value={run.agent || '—'} tone="muted" />
            <StatRow label="Runtime" value={run.runtime || '—'} tone="muted" />
          </div>

          <h3 className="hub-group-title">Decisions</h3>
          {!receipts.length ? (
            <EmptyState text="This run recorded no decisions." />
          ) : (
            <ul className="agent-receipts">
              {receipts.map((r, i) => {
                const b = receiptBadge(r);
                return (
                  <li key={r.id || i} className="agent-receipt">
                    <span className="agent-receipt-kind">{r.kind || 'decision'}</span>
                    <span className="agent-receipt-detail">{r.detail || r.decision || ''}</span>
                    {/* "enforced" vs "attribution only" is the distinction that
                        stops a reader concluding a policy was APPLIED when it
                        was merely recorded. */}
                    <Badge tone={b.tone}>{b.label}</Badge>
                  </li>
                );
              })}
            </ul>
          )}
          {(run.more || (run.receipts || []).length > RECEIPT_CAP) && (
            <p className="agent-list-note">
              Showing the first {RECEIPT_CAP} decisions.
            </p>
          )}

          <p className="hub-note">
            Best-effort operational evidence, not a compliance archive. Prompts,
            command bodies, arguments, paths and credentials are never recorded
            here — so an absent decision means it was not captured, not that it
            did not happen.
          </p>
        </>
      )}
    </Panel>
  );
}
