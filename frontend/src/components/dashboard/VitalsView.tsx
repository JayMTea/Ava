import { useCallback, useState } from 'react';
import { api } from '../../lib/api';
import { dash } from './dashApi';
import { useLiveResource } from '../../hooks/useLive';
import {
  BarList, Donut, EmptyState, Gauge, InfoTip, Panel, RangeSelector, Skeleton, StatCard, StatusPill, TimeSeries,
  fmtInt, fmtNum,
} from './primitives';
import { RANGE_MAP, type RangeKey } from './ranges';
import { METRICS } from './metrics';

export function VitalsView() {
  const summary = useLiveResource(useCallback(() => dash.perfSummary(), []), 10000);
  const [tokRange, setTokRange] = useState<RangeKey>('day');
  const tr = RANGE_MAP[tokRange];
  const tokSeries = useLiveResource(
    useCallback(() => dash.perfSeries('tokens_per_sec', tr.bucket, tr.since, undefined, 'llm'), [tr]), tr.pollMs);
  const cost = useLiveResource(useCallback(() => dash.perfCost('7d', 'app'), []), 30000);
  const conns = useLiveResource(useCallback(() => dash.connectors(), []), 15000);
  const budget = useLiveResource(useCallback(() => dash.budget(), []), 30000);
  const [hwRange, setHwRange] = useState<RangeKey>('day');
  const hr = RANGE_MAP[hwRange];
  const hw = useLiveResource(useCallback(() => dash.hwHistory(hr.since, hr.bucket), [hr]), hr.pollMs);
  // The gauges + the watts readout show the instantaneous reading from the SAME
  // live snapshot (/api/hardware) the floating hardware bubble uses — so the two
  // never disagree. (The charted range below still uses the bucketed history.)
  const live = useLiveResource(useCallback(() => api.hardware(), []), 3000);

  const s = summary.data?.summary;
  const llm = s?.llm || {};
  const llmLabels = Object.keys(llm);

  // aggregate tok/s across models (avg of averages, weighted by count)
  let tokAvg: number | null = null;
  let tokN = 0, tokSum = 0, failovers = 0;
  for (const l of llmLabels) {
    const st = llm[l];
    if (st.tokens_per_sec) { tokSum += st.tokens_per_sec.avg * st.count; tokN += st.count; }
    failovers += st.failovers || 0;
  }
  if (tokN) tokAvg = tokSum / tokN;
  const ttft = llmLabels.map((l) => llm[l].ttft_ms?.avg).filter((v): v is number => v != null);
  const ttftAvg = ttft.length ? ttft.reduce((a, b) => a + b, 0) / ttft.length : null;

  const samples = hw.data?.samples || [];
  const lh = live.data;   // live instantaneous hardware snapshot (gauges + watts)
  const hwPoints = samples.map((x) => ({
    t: x.ts, 'GPU util': x.gpu_util ?? 0, 'GPU temp': x.gpu_temp ?? 0,
    'GPU power': x.gpu_power ?? 0, 'Mem %': x.mem_used_pct ?? 0, CPU: x.cpu ?? 0,
  }));

  const modelShare = llmLabels.map((l) => ({ name: l, value: llm[l].count }));
  const genBars = [
    { name: 'Image render s', value: s?.image?.render_seconds?.avg ?? 0 },
    { name: 'Video render s', value: s?.video?.render_seconds?.avg ?? 0 },
    { name: 'Upscale s', value: s?.upscale?.seconds?.avg ?? 0 },
  ].filter((b) => b.value > 0);
  const costBy = Object.entries(cost.data?.by || {}).map(([name, v]) => ({ name, value: v.energy_kwh }));
  // Connected apps: external apps only (same rule as the Connectors page —
  // core/inference/media are infrastructure, not apps), live from the registry:
  // an app added or removed in Connectors appears/disappears here immediately,
  // with an honest "no metrics yet" until its first record arrives.
  const apps = (conns.data?.connectors || [])
    .filter((c) => !['core', 'inference', 'media'].includes(c.kind));

  return (
    <div className="db-view">
      <div className="db-view-head">
        <h2>Vitals</h2>
        <span className="db-view-sub">Ava's performance across all apps</span>
      </div>

      {/* KPI strip */}
      <div className="db-kpis">
        <StatCard label="Spend (7d)" tone="accent" help={METRICS.spend}
          value={cost.data ? `$${fmtNum(cost.data.spend_usd, 2)}` : '—'}
          hint={cost.data?.by ? `${Object.keys(cost.data.by).length} sources` : 'code API cost'} />
        <StatCard label={cost.data && cost.data.energy_state !== 'measured' ? 'Energy (7d, est.)' : 'Energy (7d)'} tone="accent" help={METRICS.energy}
          value={cost.data ? fmtNum(cost.data.energy_kwh, 3) : '—'} unit="kWh"
          hint={cost.data
            ? (cost.data.energy_state === 'measured'
              ? (cost.data.energy_usd != null ? `≈ $${fmtNum(cost.data.energy_usd, 2)} · measured` : `${fmtNum(cost.data.avg_gpu_watts, 0)} W measured avg`)
              : cost.data.energy_state === 'partial'
                // Recent hours are sampled; anything past perf.hot_window is nominal.
                // Saying "no power sensor" here would be a different wrong answer.
                ? `part measured — recent hours sampled at ${fmtNum(cost.data.avg_gpu_watts, 0)} W, older nominal`
                : `estimate — no GPU power sensor (nominal ${fmtNum(cost.data.avg_gpu_watts, 0)} W)`)
            : 'GPU energy'} />
        <StatCard label="Throughput" value={fmtNum(tokAvg, 1)} unit="tok/s" help={METRICS.throughput}
          tone={tokAvg == null ? 'default' : tokAvg < 15 ? 'warn' : 'ok'}
          hint={`${fmtInt(tokN)} completions`} />
        <StatCard label="TTFT" value={fmtNum(ttftAvg == null ? null : ttftAvg / 1000, 2)} unit="s" hint="time to first token" help={METRICS.ttft} />
        <StatCard label="Renders" value={fmtInt((s?.image?.count || 0) + (s?.video?.count || 0))}
          hint={`${fmtInt(s?.upscale?.count || 0)} upscales`} help={METRICS.renders} />
        <StatCard label="Route Errors" value={fmtInt(failovers)}
          tone={failovers ? 'warn' : 'ok'} hint="primary → fallback" help={METRICS.routeErrors} />
      </div>

      {/* Budget meter — only when a budget is configured (Setup → Budgets) */}
      {budget.data && (budget.data.budgets.daily_usd || budget.data.budgets.daily_kwh) && (
        <Panel title="Today's budget" subtitle="Spend & energy against your caps — set on the Setup → Budgets page">
          <div style={{ display: 'grid', gap: 14, gridTemplateColumns: '1fr 1fr' }}>
            {budget.data.budgets.daily_usd != null && (() => {
              const used = budget.data.daily_spend_usd, cap = budget.data.budgets.daily_usd!;
              const pct = Math.min(100, Math.round((used / cap) * 100));
              const col = pct >= 100 ? 'var(--err)' : pct >= 80 ? 'var(--warn)' : 'var(--ok)';
              return (
                <div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 'var(--fs-sm)', marginBottom: 5 }}>
                    <span style={{ color: 'var(--muted)' }}>Cloud spend</span>
                    <span><b style={{ color: col }}>${used.toFixed(2)}</b> <span style={{ color: 'var(--muted)' }}>/ ${cap}</span></span>
                  </div>
                  <div style={{ height: 8, borderRadius: 999, background: 'var(--panel2)', overflow: 'hidden' }}>
                    <div style={{ width: `${pct}%`, height: '100%', background: col }} />
                  </div>
                </div>
              );
            })()}
            {budget.data.budgets.daily_kwh != null && (() => {
              const used = budget.data.daily_energy_kwh, cap = budget.data.budgets.daily_kwh!;
              const pct = Math.min(100, Math.round((used / cap) * 100));
              const col = pct >= 100 ? 'var(--err)' : pct >= 80 ? 'var(--warn)' : 'var(--ok)';
              return (
                <div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 'var(--fs-sm)', marginBottom: 5 }}>
                    <span style={{ color: 'var(--muted)' }}>GPU energy{budget.data.power_measured ? '' : ' (est.)'}</span>
                    <span><b style={{ color: col }}>{used.toFixed(2)}</b> <span style={{ color: 'var(--muted)' }}>/ {cap} kWh</span></span>
                  </div>
                  <div style={{ height: 8, borderRadius: 999, background: 'var(--panel2)', overflow: 'hidden' }}>
                    <div style={{ width: `${pct}%`, height: '100%', background: col }} />
                  </div>
                </div>
              );
            })()}
          </div>
        </Panel>
      )}

      {/* Row: inference throughput + model share */}
      <div className="db-grid db-grid-2">
        <Panel title={<>Inference throughput <InfoTip text={METRICS.throughputSeries} label="Inference throughput" /></>}
          subtitle={`tokens/sec by model · ${tr.label}`}
          right={<RangeSelector value={tokRange} onChange={setTokRange} />}>
          {tokSeries.loading ? <Skeleton /> :
            tokSeries.data && tokSeries.data.points.length ?
              <TimeSeries points={tokSeries.data.points} series={tokSeries.data.series} unit=""
                xTickFmt={tr.tick} xTipFmt={tr.tip} /> :
              <EmptyState text={(s?.llm && Object.keys(s.llm).length) || s?.records
                ? 'Turns are recorded, but no token-rate samples in this range — the agent runtime reports tokens/sec only when its endpoint returns usage.'
                : 'No inference recorded yet — chat with Ava to populate this.'} />}
        </Panel>
        <Panel title={<>Model routing <InfoTip text={METRICS.modelShare} label="Model routing" /></>}
          subtitle="share of completions served">
          {modelShare.length ? <Donut data={modelShare} /> :
            <EmptyState text="No completions yet." />}
        </Panel>
      </div>

      {/* Row: generation performance + reliability */}
      <div className="db-grid db-grid-2">
        <Panel title="Generation performance" subtitle="avg seconds per render pass">
          {genBars.length ? <BarList data={genBars} unit="s" /> :
            <EmptyState text="No image/video renders recorded yet." />}
        </Panel>
        <Panel title="Energy by app (7d)" subtitle="estimated kWh">
          {costBy.some((c) => c.value > 0) ? <BarList data={costBy} unit=" kWh" /> :
            <EmptyState text="No energy data yet." />}
        </Panel>
      </div>

      {/* Connected apps — auto-populates from the connector registry */}
      <Panel title="Connected apps"
        subtitle="live from Connectors — each app appears here the moment it's added">
        {apps.length ? (
          <div className="db-conn-grid">
            {apps.map((c) => {
              const activity = cost.data?.by?.[c.perf_app];
              return (
                <div key={c.id} className="db-conn">
                  <div className="db-conn-top">
                    <span className="db-conn-name">{c.label}</span>
                    {c.status ? <StatusPill status={c.status} /> :
                      <span className="db-conn-kind">{c.kind}</span>}
                  </div>
                  <div className="db-conn-meta">
                    {activity?.n
                      ? [`${fmtInt(activity.n)} calls (7d)`,
                        activity.energy_kwh > 0 ? `${fmtNum(activity.energy_kwh, 3)} kWh` : '',
                        c.actions.length ? `${c.actions.length} actions` : '']
                        .filter(Boolean).join(' · ')
                      : c.perf_present
                        ? 'connected · no activity in 7d'
                        : 'connected · no metrics yet — history starts with its first call'}
                  </div>
                </div>
              );
            })}
          </div>
        ) : conns.data
          ? <EmptyState text="No apps connected yet — add one on the Connectors page and it will appear here." />
          : <Skeleton />}
      </Panel>

      {/* Hardware */}
      <Panel title={<>Hardware <InfoTip text={METRICS.hardwareSeries} label="Hardware telemetry" /></>}
        subtitle={`live device telemetry · ${hr.label}`}
        right={
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {lh?.gpu.power != null && (
              <span className="db-panel-right-note">{Math.round(lh.gpu.power)} W</span>
            )}
            <RangeSelector value={hwRange} onChange={setHwRange} />
          </div>
        }>
        <div className="db-gauges">
          <Gauge value={lh?.gpu.util ?? null} label="GPU util" help={METRICS.gpuUtil} />
          <Gauge value={lh?.gpu.temp ?? null} label="GPU temp" unit="°" max={100} warnAt={78} critAt={88} help={METRICS.gpuTemp} />
          <Gauge value={lh?.mem.used_pct ?? null} label="Memory" warnAt={85} critAt={95} help={METRICS.memory} />
          <Gauge value={lh?.cpu.util ?? null} label="CPU util" help={METRICS.cpu} />
        </div>
        {hwPoints.length > 1 ? (
          <div style={{ marginTop: 12 }}>
            <TimeSeries points={hwPoints} series={['GPU util', 'GPU temp', 'Mem %', 'CPU']} height={200}
              xTickFmt={hr.tick} xTipFmt={hr.tip} />
          </div>
        ) : <EmptyState text={hw.loading ? 'Loading…' : 'Collecting hardware samples…'} />}
      </Panel>
    </div>
  );
}
