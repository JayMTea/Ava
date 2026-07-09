import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, learning } from '../../lib/api';
import { InfoTip } from '../dashboard/primitives';
import { METRICS } from '../dashboard/metrics';
import type { LearnContext, LearningCycle, Proposal, StagedChange } from '../../lib/types';
import { Icon } from '../../lib/icons';

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────
function relTime(ts?: string): string {
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  return d.toLocaleString();
}

function ago(ts?: string): string {
  if (!ts) return '—';
  const d = new Date(ts).getTime();
  if (isNaN(d)) return '—';
  const s = Math.max(0, (Date.now() - d) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

const APP_META: Record<string, { label: string; sub: string }> = {
  ava: { label: 'Ava', sub: 'the assistant · self-editing & chat learning' },
};
function appMeta(key: string) {
  return APP_META[key] || { label: key, sub: 'connected app' };
}
const APP_COLORS = ['#007acc', '#4f8a8b', '#7c6f9f', '#b08968', '#5e8c6a', '#9a6a8c', '#6a7fa8'];
function appColor(key: string): string {
  let h = 0;
  for (const c of key) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return APP_COLORS[h % APP_COLORS.length];
}

function statusOf(p: Proposal): string {
  return p.status || 'pending';
}
function isDone(p: Proposal): boolean {
  return p.applied === true || p.status === 'completed' || p.status === 'approved';
}
function isCodeChange(p: Proposal): boolean {
  return p.type === 'code_change' || (p.staged_changes?.length || 0) > 0;
}

// A proposal tagged with the learning context + app it belongs to.
interface EnrichedProposal extends Proposal {
  ctx: LearnContext;
  app: string;
  cycleTs?: string;
}
interface AppBucket {
  key: string;
  proposals: EnrichedProposal[];
  lastTs?: string;
}

// ─────────────────────────────────────────────────────────────────────────────
// Diff + proposal (drill-down detail)
// ─────────────────────────────────────────────────────────────────────────────
function DiffBlock({ change }: { change: StagedChange }) {
  const [open, setOpen] = useState(false);
  const lines = (change.diff || '').split('\n');
  return (
    <div className="ln-file">
      <button className="ln-file-head" onClick={() => setOpen((o) => !o)}>
        <Icon name={open ? 'expand' : 'code'} />
        <span className="ln-file-path">{change.path}</span>
        <span className={'ln-file-stat s-' + (change.status || 'M')}>{change.status || 'M'}</span>
      </button>
      {open && change.diff && (
        <pre className="ln-diff">
          {lines.map((l, i) => {
            let cls = 'dctx';
            if (l.startsWith('+') && !l.startsWith('+++')) cls = 'dadd';
            else if (l.startsWith('-') && !l.startsWith('---')) cls = 'ddel';
            else if (l.startsWith('@@')) cls = 'dhk';
            else if (l.startsWith('diff ') || l.startsWith('index ')) cls = 'dh';
            return (
              <div key={i} className={cls}>
                {l || ' '}
              </div>
            );
          })}
        </pre>
      )}
    </div>
  );
}

function ProposalCard({ proposal, onChanged }: { proposal: EnrichedProposal; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [status, setStatus] = useState(statusOf(proposal));
  const [completedBy, setCompletedBy] = useState<string | null>(proposal.completed_by ?? null);
  const [feedback, setFeedback] = useState<number | null>(proposal.feedback ?? null);
  const [open, setOpen] = useState(false);
  const ctx = proposal.ctx;

  const act = async (
    fn: () => Promise<{ ok: boolean; message?: string; error?: string; restart?: boolean; branch?: string }>,
    next?: string,
  ) => {
    setBusy(true);
    setNote('');
    try {
      const r = await fn();
      if (r.ok) {
        if (next) setStatus(next);
        if (next === 'completed') setCompletedBy(proposal.requested_by || 'Ava');
        setNote((r.message || 'Done') + (r.restart ? ' · Ava will restart to apply.' : ''));
        onChanged();
      } else setNote(r.error || 'Failed');
    } catch (e) {
      setNote((e as Error).message);
    }
    setBusy(false);
  };

  const rate = async (rating: 0 | 1) => {
    setFeedback(rating);
    try {
      await learning.feedback(ctx, proposal.id, rating);
    } catch {
      /* ignore */
    }
  };

  const files = proposal.staged_changes || [];
  const code = isCodeChange(proposal);
  const done = status === 'completed' || proposal.applied === true;
  const rejected = status === 'rejected';
  const who = completedBy || proposal.completed_by || (proposal.applied ? proposal.requested_by || 'Ava' : null);
  const where = proposal.applied_branch
    ? `branch ${proposal.applied_branch}`
    : proposal.applied_commit
      ? `commit ${proposal.applied_commit}`
      : '';

  return (
    <div className={'ln-prop status-' + (done ? 'completed' : status)}>
      <div className="ln-prop-head">
        <div className="ln-prop-title">{proposal.title || 'Suggestion'}</div>
        <div className="ln-tags">
          <span className="ln-tag app" style={{ ['--c' as string]: appColor(proposal.app) }}>
            {appMeta(proposal.app).label}
          </span>
          <span className="ln-tag">{code ? 'code' : proposal.type || 'idea'}</span>
          {proposal.risk && <span className={'ln-tag r-' + proposal.risk}>{proposal.risk}</span>}
          <span className={'ln-tag st-' + (done ? 'completed' : status)}>{done ? 'completed' : status}</span>
        </div>
      </div>
      {proposal.description && <p className="ln-prop-desc">{proposal.description}</p>}
      {proposal.why && !done && (
        <p className="ln-prop-why">
          <span>Why</span> {proposal.why}
        </p>
      )}

      {files.length > 0 && (
        <div className="ln-files">
          <button className="ln-files-h" onClick={() => setOpen((o) => !o)}>
            <Icon name={open ? 'expand' : 'code'} />
            {files.length} file{files.length > 1 ? 's' : ''}
            {proposal.added != null && <span className="ln-plus"> +{proposal.added}</span>}
            {proposal.removed != null && <span className="ln-minus"> -{proposal.removed}</span>}
            <span className="ln-files-toggle">{open ? 'hide diff' : 'view diff'}</span>
          </button>
          {open && files.map((c, i) => <DiffBlock key={i} change={c} />)}
        </div>
      )}

      {/* Completed / rejected banner replaces the approve/reject gate. */}
      {done ? (
        <div className="ln-done">
          <Icon name="check" />
          <span>
            Completed{who ? ' by ' + who : ''}
            {proposal.approved_by && proposal.approved_by !== who ? ' · approved by ' + proposal.approved_by : ''}
            {where ? ' · ' + where : ''}
            {proposal.completed_at ? ' · ' + ago(proposal.completed_at) : ''}
          </span>
          <div className="ln-done-fb">
            helpful?
            <button className={'ln-fb' + (feedback === 1 ? ' on' : '')} onClick={() => rate(1)}>
              <Icon name="check" /> Yes
            </button>
            <button className={'ln-fb' + (feedback === 0 ? ' on' : '')} onClick={() => rate(0)}>
              <Icon name="close" /> No
            </button>
          </div>
        </div>
      ) : rejected ? (
        <div className="ln-done rejected">
          <Icon name="close" />
          <span>Rejected</span>
        </div>
      ) : (
        <div className="ln-prop-actions">
          <button className="ln-btn approve" disabled={busy} onClick={() => act(() => learning.apply(ctx, proposal.id), 'completed')}>
            <Icon name="check" />
            {code ? 'Approve & apply' : 'Approve'}
          </button>
          <button className="ln-btn reject" disabled={busy} onClick={() => act(() => learning.reject(ctx, proposal.id), 'rejected')}>
            <Icon name="close" />
            Reject
          </button>
        </div>
      )}
      {note && <div className="ln-note">{note}</div>}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Dashboard pieces
// ─────────────────────────────────────────────────────────────────────────────
function StatTile({ label, value, tone, help }: { label: string; value: number | string; tone?: string; help?: string }) {
  return (
    <div className={'dash-stat' + (tone ? ' t-' + tone : '')}>
      <div className="dash-stat-v">{value}</div>
      <div className="dash-stat-l">{label}{help && <InfoTip text={help} label={label} />}</div>
    </div>
  );
}

function AppCard({ bucket, onOpen }: { bucket: AppBucket; onOpen: () => void }) {
  const meta = appMeta(bucket.key);
  const ps = bucket.proposals;
  const pending = ps.filter((p) => statusOf(p) === 'pending').length;
  const applied = ps.filter(isDone).length;
  const codeN = ps.filter(isCodeChange).length;
  const recN = ps.length - codeN;
  return (
    <button className="app-card" onClick={onOpen}>
      <div className="app-card-top">
        <span className="app-mono" style={{ background: appColor(bucket.key) }}>
          {meta.label.charAt(0).toUpperCase()}
        </span>
        <div className="app-card-id">
          <div className="app-card-name">{meta.label}</div>
          <div className="app-card-sub">{meta.sub}</div>
        </div>
        {pending > 0 && <span className="app-card-pending">{pending}</span>}
      </div>
      <div className="app-card-stats">
        <span>
          <b>{codeN}</b> code
        </span>
        <span>
          <b>{recN}</b> ideas
        </span>
        <span>
          <b>{applied}</b> applied
        </span>
      </div>
      <div className="app-card-foot">
        <span>
          {ps.length} total · {ago(bucket.lastTs)}
        </span>
        <span className="app-card-open">Open →</span>
      </div>
    </button>
  );
}

function GateRow({ p, onOpen }: { p: EnrichedProposal; onOpen: () => void }) {
  return (
    <button className="gate-row" onClick={onOpen}>
      <span className="gate-app" style={{ background: appColor(p.app) }}>
        {appMeta(p.app).label.charAt(0).toUpperCase()}
      </span>
      <div className="gate-main">
        <div className="gate-title">{p.title || 'Proposal'}</div>
        <div className="gate-meta">
          {appMeta(p.app).label} · {isCodeChange(p) ? 'code change' : p.type || 'idea'}
          {p.risk ? ' · ' + p.risk + ' risk' : ''} · {ago(p.cycleTs)}
        </div>
      </div>
      <span className="gate-cta">Review →</span>
    </button>
  );
}

// A cycle rendered inside an app's detail view (proposals already filtered to app).
function CycleCard({
  cycle,
  proposals,
  onChanged,
}: {
  cycle: LearningCycle;
  proposals: EnrichedProposal[];
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(true);
  const [showPatterns, setShowPatterns] = useState(false);
  const pending = proposals.filter((p) => statusOf(p) === 'pending').length;
  return (
    <div className="ln-cycle">
      <button className="ln-cycle-head" onClick={() => setOpen((o) => !o)}>
        <Icon name={open ? 'expand' : 'code'} />
        <span className="ln-cycle-time">{relTime(cycle.timestamp)}</span>
        <span className="ln-cycle-badge">
          {proposals.length} proposal{proposals.length === 1 ? '' : 's'}
          {pending > 0 && <em> · {pending} pending</em>}
        </span>
      </button>
      {open && (
        <div className="ln-cycle-body">
          {cycle.patterns && Object.keys(cycle.patterns).length > 0 && (
            <div className="ln-patterns">
              <button className="ln-patterns-toggle" onClick={() => setShowPatterns((s) => !s)}>
                {showPatterns ? 'Hide' : 'Show'} analysis
              </button>
              {showPatterns && <pre className="ln-patterns-json">{JSON.stringify(cycle.patterns, null, 2)}</pre>}
            </div>
          )}
          {proposals.map((p) => (
            <ProposalCard key={p.id} proposal={p} onChanged={onChanged} />
          ))}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main
// ─────────────────────────────────────────────────────────────────────────────
export function LearningView({ embedded = false }: { embedded?: boolean } = {}) {
  const [codeCycles, setCodeCycles] = useState<LearningCycle[]>([]);
  const [chatCycles, setChatCycles] = useState<LearningCycle[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selApp, setSelApp] = useState<string | null>(null);
  const [appTab, setAppTab] = useState<LearnContext>('code');
  const [running, setRunning] = useState(false);
  const [runNote, setRunNote] = useState('');
  // Apps Ava actually has a connection to (the connector registry). The learning
  // store can carry `project` tags for repos Ava was pointed at in the past
  // (e.g. before apps were decoupled); we only surface Ava herself + apps that
  // are currently connected, so the grid can't leak stale/foreign projects.
  const [connectedApps, setConnectedApps] = useState<Set<string>>(new Set());

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [code, chat, apps] = await Promise.all([
        learning.state('code').catch(() => ({ cycles: [] as LearningCycle[] })),
        learning.state('chat').catch(() => ({ cycles: [] as LearningCycle[] })),
        api.apps().catch(() => ({ apps: [] as { id: string }[] })),
      ]);
      setCodeCycles(code.cycles || []);
      setChatCycles(chat.cycles || []);
      setConnectedApps(new Set((apps.apps || []).map((a) => a.id)));
    } catch (e) {
      setError((e as Error).message);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Trigger a learning cycle now (local-first analysis of recent activity).
  const runNow = useCallback(async () => {
    setRunning(true);
    setRunNote('');
    try {
      const r = await learning.run();
      const n = (r.code_proposals || 0) + (r.chat_proposals || 0);
      setRunNote(n > 0 ? `Added ${n} proposal${n === 1 ? '' : 's'}` : 'Analyzed — no new proposals');
      await load();
    } catch (e) {
      setRunNote((e as Error).message);
    }
    setRunning(false);
  }, [load]);

  // Flatten every proposal, tagged with its context + app. Only Ava's own work
  // and proposals belonging to a currently-connected app are kept — anything
  // tagged with a project Ava is no longer connected to is left out of the view.
  const allProps = useMemo<EnrichedProposal[]>(() => {
    const out: EnrichedProposal[] = [];
    const keep = (app: string) => app === 'ava' || connectedApps.has(app);
    for (const cy of codeCycles)
      for (const p of cy.proposals || []) {
        const app = p.project || 'ava';
        if (keep(app)) out.push({ ...p, ctx: 'code', app, cycleTs: cy.timestamp });
      }
    for (const cy of chatCycles)
      for (const p of cy.proposals || []) out.push({ ...p, ctx: 'chat', app: 'ava', cycleTs: cy.timestamp });
    return out;
  }, [codeCycles, chatCycles, connectedApps]);

  const buckets = useMemo<AppBucket[]>(() => {
    const m = new Map<string, AppBucket>();
    // Ava is always a first-class app — she's the assistant AND can recursively
    // edit her own code — so her card shows even before she has any proposals.
    m.set('ava', { key: 'ava', proposals: [] });
    for (const p of allProps) {
      let b = m.get(p.app);
      if (!b) {
        b = { key: p.app, proposals: [] };
        m.set(p.app, b);
      }
      b.proposals.push(p);
      if (!b.lastTs || (p.cycleTs && p.cycleTs > b.lastTs)) b.lastTs = p.cycleTs;
    }
    return [...m.values()].sort((a, b) =>
      a.key === 'ava' ? -1 : b.key === 'ava' ? 1 : (b.lastTs || '').localeCompare(a.lastTs || ''),
    );
  }, [allProps]);

  const pendingGates = useMemo(
    () =>
      allProps
        .filter((p) => statusOf(p) === 'pending')
        .sort((a, b) => (b.cycleTs || '').localeCompare(a.cycleTs || '')),
    [allProps],
  );
  const applied = allProps.filter(isDone).length;
  const codeChanges = allProps.filter(isCodeChange).length;

  const openApp = (key: string) => {
    setSelApp(key);
    setAppTab('code');
  };

  // Cycles for the selected app + tab, with proposals filtered to the app.
  const appCycles = useMemo(() => {
    if (!selApp) return [];
    const src = appTab === 'chat' ? chatCycles : codeCycles;
    return src
      .map((cy) => ({
        cy,
        props: (cy.proposals || [])
          .filter((p) => (appTab === 'chat' ? 'ava' : p.project || 'ava') === selApp)
          .map((p) => ({ ...p, ctx: appTab, app: selApp, cycleTs: cy.timestamp }) as EnrichedProposal),
      }))
      .filter((x) => x.props.length > 0);
  }, [selApp, appTab, codeCycles, chatCycles]);

  return (
    <div className={'learning' + (embedded ? ' embedded' : ' view-scroll')}>
      <div className="lv-inner dash">
        <div className="dash-head">
          <div>
            {!(embedded && !selApp) && (
              <h2>{selApp ? appMeta(selApp).label : 'Control Center'}</h2>
            )}
            <p className="lv-sub">
              {selApp
                ? appMeta(selApp).sub
                : 'Review and approve the changes Ava proposes across every connected app.'}
            </p>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {runNote && <span className="lv-run-note" style={{ fontSize: 12, opacity: 0.7 }}>{runNote}</span>}
            {!selApp && (
              <button
                className="lv-refresh"
                onClick={runNow}
                disabled={running}
                title="Run a learning cycle now — analyze recent activity locally"
                style={{ width: 'auto', padding: '0 12px', gap: 6, display: 'inline-flex', alignItems: 'center' }}
              >
                <Icon name={running ? 'refresh' : 'bot'} />
                <span style={{ fontSize: 13 }}>{running ? 'Analyzing…' : 'Run now'}</span>
              </button>
            )}
            <button className="lv-refresh" onClick={load} title="Refresh" aria-label="Refresh">
              <Icon name="refresh" />
            </button>
          </div>
        </div>

        {loading ? (
          <div className="lv-state">Loading…</div>
        ) : error ? (
          <div className="lv-state error">{error}</div>
        ) : selApp ? (
          <>
            <button className="dash-back" onClick={() => setSelApp(null)}>
              ← All apps
            </button>
            {selApp === 'ava' && (
              <div className="lv-tabs">
                <button className={'lv-tab' + (appTab === 'code' ? ' active' : '')} onClick={() => setAppTab('code')}>
                  <Icon name="code" /> Code
                </button>
                <button className={'lv-tab' + (appTab === 'chat' ? ' active' : '')} onClick={() => setAppTab('chat')}>
                  <Icon name="bot" /> Chat
                </button>
              </div>
            )}
            {appCycles.length === 0 ? (
              <div className="lv-state">
                No {appTab} activity for {appMeta(selApp).label} yet.
              </div>
            ) : (
              appCycles.map(({ cy, props }) => (
                <CycleCard key={cy.id + appTab} cycle={cy} proposals={props} onChanged={load} />
              ))
            )}
          </>
        ) : (
          <>
            <div className="dash-stats">
              <StatTile label="Approval gates" value={pendingGates.length} tone={pendingGates.length ? 'attn' : undefined} help={METRICS.approvalGates} />
              <StatTile label="Code changes" value={codeChanges} help={METRICS.codeChanges} />
              <StatTile label="Completed" value={applied} tone="ok" help={METRICS.completed} />
              <StatTile label="Apps" value={buckets.length} help={METRICS.apps} />
            </div>

            {pendingGates.length > 0 && (
              <section className="dash-sec">
                <div className="dash-sec-h">
                  <Icon name="lock" /> Awaiting your approval
                  <span className="dash-sec-count">{pendingGates.length}</span>
                </div>
                <div className="gate-list">
                  {pendingGates.slice(0, 6).map((p) => (
                    <GateRow key={p.id} p={p} onOpen={() => openApp(p.app)} />
                  ))}
                </div>
              </section>
            )}

            <section className="dash-sec">
              <div className="dash-sec-h">
                <Icon name="panel" /> Apps
              </div>
              {buckets.length === 0 ? (
                <div className="lv-state">No activity yet. Ava will add proposals as she works across apps.</div>
              ) : (
                <div className="app-grid">
                  {buckets.map((b) => (
                    <AppCard key={b.key} bucket={b} onOpen={() => openApp(b.key)} />
                  ))}
                </div>
              )}
            </section>
          </>
        )}
      </div>
    </div>
  );
}
