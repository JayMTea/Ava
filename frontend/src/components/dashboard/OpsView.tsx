import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { dash } from './dashApi';
import type { Alert, JobRow, TurnRow } from './dashApi';
import { useEventStream, useLiveResource } from '../../hooks/useLive';
import { ProgressBar } from '../../lib/ProgressBar';
import { LearningView } from '../learning/LearningView';
import { EmptyState, Panel, StatusPill, ago, fmtClock, fmtInt } from './layout';
import { BarList, StatCard } from './charts';
import { METRICS } from './metrics';

type Overlay = Record<string, { status?: string; progress?: number; stage?: string; step_count?: number; tools?: number }>;

// One event pushed to Ava by a user's device/sensor connector app (device.event).
type DevEvent = { seq: number; ts: number; cid: string; name: string; type?: string;
  value?: number | string; unit?: string; message?: string; severity?: string; notify?: boolean };

// Optional proactive voice: speak a device event aloud in this browser. Uses the
// Web Speech API (no server voice subprocess), the seam docs/DEVICE_CONNECTORS.md
// flagged for future announcements. Only fires for notify/warn/critical events.
function announceDeviceEvent(e: DevEvent) {
  if (typeof window === 'undefined' || !('speechSynthesis' in window)) return;
  const val = e.value !== undefined ? ` ${e.value}${e.unit ? ' ' + e.unit : ''}` : '';
  const text = e.message || `${e.cid}: ${e.name}${val}`;
  try { window.speechSynthesis.speak(new SpeechSynthesisUtterance(text)); } catch { /* ignore */ }
}

export function OpsView() {
  const summary = useLiveResource(useCallback(() => dash.opsSummary(), []), 6000);
  const turnsRes = useLiveResource(useCallback(() => dash.turns(false, 40), []), 6000);
  const jobsRes = useLiveResource(useCallback(() => dash.jobs(undefined, undefined, 60), []), 6000);
  const services = useLiveResource(useCallback(() => dash.services(), []), 12000);
  const schedule = useLiveResource(useCallback(() => dash.schedule(), []), 30000);
  const tools = useLiveResource(useCallback(() => dash.tools(12), []), 15000);
  const alertsRes = useLiveResource(useCallback(() => dash.alerts(), []), 20000);
  const conns = useLiveResource(useCallback(() => dash.connectors(), []), 30000);

  const [turnOv, setTurnOv] = useState<Overlay>({});
  const [jobOv, setJobOv] = useState<Overlay>({});
  const [liveAlerts, setLiveAlerts] = useState<Alert[] | null>(null);
  const [deviceEvents, setDeviceEvents] = useState<DevEvent[]>([]);
  const [seg, setSeg] = useState<'live' | 'control'>('live');
  const [speakAlerts, setSpeakAlerts] = useState(() => {
    try { return localStorage.getItem('ava.speakDeviceAlerts') === '1'; } catch { return false; }
  });
  // A ref so the (once-created, []-deps) SSE handler reads the live toggle value.
  const speakRef = useRef(speakAlerts);
  useEffect(() => {
    speakRef.current = speakAlerts;
    try { localStorage.setItem('ava.speakDeviceAlerts', speakAlerts ? '1' : '0'); } catch { /* ignore */ }
  }, [speakAlerts]);

  useEventStream('/api/stream/ops', useMemo(() => ({
    'turn.update': (d: unknown) => {
      const t = d as { id: string; status?: string; step_count?: number; tools?: number };
      setTurnOv((o) => ({ ...o, [t.id]: { ...o[t.id], status: t.status, step_count: t.step_count, tools: t.tools } }));
    },
    'job.update': (d: unknown) => {
      const j = d as { id: string; status?: string; progress?: number; stage?: string };
      setJobOv((o) => ({ ...o, [j.id]: { ...o[j.id], status: j.status, progress: j.progress, stage: j.stage } }));
    },
    'alert.state': (d: unknown) => setLiveAlerts(d as Alert[]),
    'alert.raise': (d: unknown) => setLiveAlerts((a) => {
      const al = d as Alert; const base = a || [];
      return base.some((x) => x.id === al.id) ? base : [...base, al];
    }),
    'alert.clear': (d: unknown) => setLiveAlerts((a) => (a || []).filter((x) => x.id !== (d as { id: string }).id)),
    'device.event': (d: unknown) => {
      const ev = d as DevEvent;
      setDeviceEvents((xs) => [ev, ...xs].slice(0, 30));
      if (speakRef.current && (ev.notify || ev.severity === 'warn' || ev.severity === 'critical')) {
        announceDeviceEvent(ev);
      }
    },
  }), []));

  const alerts = liveAlerts ?? alertsRes.data?.active ?? [];
  const criticals = alerts.filter((a) => a.severity === 'critical');

  const turns: TurnRow[] = (turnsRes.data?.turns || []).map((t) => ({
    ...t, status: turnOv[t.id]?.status ?? t.status,
    step_count: turnOv[t.id]?.step_count ?? t.step_count,
  }));
  const jobs: JobRow[] = (jobsRes.data?.jobs || []).map((j) => ({
    ...j, status: jobOv[j.id]?.status ?? j.status,
    progress: jobOv[j.id]?.progress ?? j.progress, stage: jobOv[j.id]?.stage ?? j.stage,
  }));

  const running = [
    ...turns.filter((t) => t.status === 'running').map((t) => ({ kind: 'turn' as const, t })),
    ...jobs.filter((j) => j.status === 'running').map((j) => ({ kind: 'job' as const, j })),
  ];
  const learn = summary.data?.learning;
  const pending = (learn?.code.pending || 0) + (learn?.chat.pending || 0);
  const toolBars = (tools.data?.tools || []).map((t) => ({ name: t.tool.replace(/^.*__/, ''), value: t.count }));

  return (
    <div className="db-view">
      <div className="db-view-head">
        <h2>Operations</h2>
        <span className="db-view-sub">Ava's live work, jobs &amp; control</span>
      </div>

      {criticals.length > 0 && (
        <div className="db-banner db-banner-crit">
          <strong>{criticals.length} critical alert{criticals.length > 1 ? 's' : ''}:</strong>{' '}
          {criticals.map((a) => a.message).join(' · ')}
        </div>
      )}

      <div className="db-seg" role="tablist">
        <button type="button" className={'db-seg-btn' + (seg === 'live' ? ' on' : '')} onClick={() => setSeg('live')}>Live</button>
        <button type="button" className={'db-seg-btn' + (seg === 'control' ? ' on' : '')} onClick={() => setSeg('control')}>
          Control{pending ? <span className="db-seg-badge">{pending}</span> : null}
        </button>
      </div>

      {seg === 'control' ? (
        <LearningView embedded />
      ) : (
      <>
      <div className="db-kpis">
        <StatCard label="Active turns" value={fmtInt(summary.data?.turns.running)} tone={summary.data?.turns.running ? 'accent' : 'default'} help={METRICS.activeTurns} />
        <StatCard label="Active renders" value={fmtInt(summary.data?.jobs.running)} tone={summary.data?.jobs.running ? 'accent' : 'default'} help={METRICS.activeRenders} />
        <StatCard label="Generations 24h" value={fmtInt(summary.data?.generations_24h)} help={METRICS.generations24h} />
        <StatCard label="Services up" value={services.data ? `${services.data.services.length - services.data.down}/${services.data.services.length}` : '—'}
          tone={services.data?.down ? 'err' : 'ok'} help={METRICS.servicesUp} />
        <StatCard label="Pending approvals" value={fmtInt(pending)} tone={pending ? 'warn' : 'default'} hint="learning proposals" help={METRICS.pendingApprovals} />
        <StatCard label="Alerts" value={fmtInt(alerts.length)} tone={criticals.length ? 'err' : alerts.length ? 'warn' : 'ok'} help={METRICS.alerts} />
      </div>

      {/* Live activity feed */}
      <Panel title="Live activity" subtitle="in-flight turns & renders (streaming)"
        right={<span className="db-live-dot" title="live"><i />live</span>}>
        {running.length === 0 ? <EmptyState text="Nothing running right now." /> : (
          <div className="db-feed">
            {running.map((r) => r.kind === 'turn' ? (
              <div key={r.t.id} className="db-feed-row">
                <span className="db-feed-badge badge-turn">turn</span>
                <div className="db-feed-main">
                  <div className="db-feed-title">{r.t.reply_preview || r.t.last_step?.text || 'thinking…'}</div>
                  <div className="db-feed-meta">
                    {r.t.last_step?.kind === 'tool' ? `→ ${r.t.last_step?.name}` : (r.t.last_step?.kind || 'reasoning')}
                    {' · '}{r.t.step_count} steps{r.t.tools_used?.length ? ` · ${r.t.tools_used.length} tools` : ''}
                  </div>
                </div>
              </div>
            ) : (
              <div key={r.j.id} className="db-feed-row">
                <span className="db-feed-badge badge-job">{r.j.kind || 'job'}</span>
                <div className="db-feed-main">
                  <div className="db-feed-title">{r.j.prompt?.slice(0, 80) || r.j.stage || 'rendering…'}</div>
                  <div className="db-feed-progress"><ProgressBar progress={r.j.progress} /></div>
                </div>
                <span className="db-feed-pct">{r.j.progress ? `${Math.round(r.j.progress)}%` : ''}</span>
              </div>
            ))}
          </div>
        )}
      </Panel>

      {/* Device events — pushed live by the user's connected device/sensor apps */}
      {deviceEvents.length > 0 && (
        <Panel title="Device events" subtitle="pushed live by your connected devices"
          right={
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 12 }}>
              <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 'var(--fs-xs)', color: 'var(--muted)', cursor: 'pointer' }}
                title="Speak notify/critical device events aloud in this browser (Web Speech).">
                <input type="checkbox" checked={speakAlerts} onChange={(e) => setSpeakAlerts(e.target.checked)} />
                Speak alerts
              </label>
              <span className="db-live-dot" title="live"><i />live</span>
            </span>
          }>
          <div className="db-feed">
            {deviceEvents.map((e) => (
              <div key={e.seq} className="db-feed-row">
                <span className={'db-feed-badge' + (e.severity === 'critical' ? ' badge-job' : ' badge-turn')}>
                  {e.severity || e.type || 'event'}
                </span>
                <div className="db-feed-main">
                  <div className="db-feed-title">
                    {e.cid}/{e.name}
                    {e.value !== undefined ? ` = ${e.value}${e.unit ? ' ' + e.unit : ''}` : ''}
                  </div>
                  {e.message ? <div className="db-feed-meta">{e.message}</div> : null}
                </div>
                <span className="db-feed-pct db-dim">{ago(e.ts)}</span>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {/* Job queue */}
      <Panel title="Job queue" subtitle="recent renders & upscales">
        {jobs.length === 0 ? <EmptyState text="No jobs yet." /> : (
          <div className="db-table-wrap">
            <table className="db-table">
              <thead><tr><th>Kind</th><th>Status</th><th>Source</th><th>Progress</th><th>Started</th></tr></thead>
              <tbody>
                {jobs.slice(0, 20).map((j) => (
                  <tr key={j.id}>
                    <td>{j.kind || '—'}</td>
                    <td><StatusPill status={j.status || 'unknown'} /></td>
                    <td>{j.source || '—'}</td>
                    <td style={{ minWidth: 120 }}>{j.status === 'running' ? <ProgressBar progress={j.progress} /> : (j.status === 'done' ? '100%' : '—')}</td>
                    <td className="db-dim">{ago(j.created)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <div className="db-grid db-grid-2">
        {/* Workflows */}
        <Panel title="Background workflows"
          right={<button type="button" className="db-linkbtn" onClick={() => setSeg('control')}>Review in Control →</button>}>
          <div className="db-work">
            <WorkRow label="Code learning" cycles={learn?.code.cycles} last={learn?.code.last_cycle} pending={learn?.code.pending} />
            <WorkRow label="Chat learning" cycles={learn?.chat.cycles} last={learn?.chat.last_cycle} pending={learn?.chat.pending} />
          </div>
        </Panel>

        {/* Alerts */}
        <Panel title="Alerts" subtitle={`${alerts.length} active`}>
          {alerts.length === 0 ? <EmptyState text="All clear." /> : (
            <div className="db-alerts">
              {alerts.map((a) => (
                <div key={a.id} className={`db-alert sev-${a.severity}`}>
                  <span className="db-alert-sev">{a.severity}</span>
                  <span className="db-alert-msg">{a.message}</span>
                  <span className="db-alert-val">{a.metric} {a.op} {a.threshold} (now {a.value ?? '—'})</span>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>

      <div className="db-grid db-grid-2">
        {/* Scheduled tasks */}
        <Panel title="Scheduled tasks" subtitle="systemd timers">
          {(schedule.data?.timers || []).length === 0 ? <EmptyState text="No timers found." /> : (
            <div className="db-table-wrap">
              <table className="db-table">
                <thead><tr><th>Task</th><th>Next run</th><th>Last run</th></tr></thead>
                <tbody>
                  {(schedule.data?.timers || []).slice(0, 12).map((t) => (
                    <tr key={t.unit}>
                      <td>
                        <div className="db-task">{t.description || (t.unit || '').replace('.timer', '')}</div>
                        <div className="db-dim db-task-unit">{(t.unit || '').replace('.timer', '')}</div>
                      </td>
                      <td>{t.next_time || '—'}{t.next_rel ? <span className="db-dim"> · in {t.next_rel}</span> : null}</td>
                      <td className="db-dim">{t.last_time || (t.last_rel ? `${t.last_rel} ago` : '—')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        {/* Service health */}
        <Panel title="Service health">
          <div className="db-svc-grid">
            {(services.data?.services || []).map((sv) => (
              <div key={sv.name} className={`db-svc db-svc-${sv.status}`}>
                <StatusPill status={sv.status} />
                <span className="db-svc-name">{sv.name}</span>
              </div>
            ))}
            {!services.data && <EmptyState text="Probing services…" />}
          </div>
        </Panel>
      </div>

      {/* Connectors */}
      <Panel title="Connectors"
        subtitle={`${conns.data?.connectors.length || 0} registered · ${conns.data?.action_count || 0} agent actions`}>
        {conns.data?.connectors.length ? (
          <div className="db-conn-grid">
            {conns.data.connectors.map((c) => (
              <div key={c.id} className="db-conn">
                <div className="db-conn-top">
                  <span className="db-conn-name">{c.label}</span>
                  <span className="db-conn-kind">{c.kind}</span>
                </div>
                <div className="db-conn-meta">
                  {[c.has_service && 'health', c.has_perf && 'perf',
                    c.egress_routes ? `${c.egress_routes} egress` : '',
                    c.actions.length ? `${c.actions.length} actions` : '']
                    .filter(Boolean).join(' · ') || '—'}
                </div>
              </div>
            ))}
          </div>
        ) : conns.error && !conns.data
          ? <EmptyState text="Couldn’t load connectors — check the bridge is reachable." />
          : <EmptyState text="No connectors registered." />}
      </Panel>

      {/* Tool usage */}
      <Panel title="Tool usage" subtitle={`${tools.data?.total_calls || 0} calls tracked (session)`}>
        {toolBars.length ? <BarList data={toolBars} /> : <EmptyState text="No tool calls yet." />}
      </Panel>
      </>
      )}
    </div>
  );
}

function WorkRow({ label, cycles, last, pending }: { label: string; cycles?: number; last?: string | null; pending?: number }) {
  return (
    <div className="db-work-row">
      <div className="db-work-main">
        <div className="db-work-label">{label}</div>
        <div className="db-work-meta">{cycles || 0} cycles · last {last ? fmtClock(Date.parse(last) / 1000) : 'never'}</div>
      </div>
      {pending ? <span className="db-badge db-badge-warn">{pending} pending</span> : <span className="db-badge db-badge-ok">clear</span>}
    </div>
  );
}
