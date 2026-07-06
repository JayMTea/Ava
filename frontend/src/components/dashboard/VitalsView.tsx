import { useCallback } from 'react';
import { dash } from './dashApi';
import { useLiveResource } from '../../hooks/useLive';
import {
  BarList, Donut, EmptyState, Gauge, Panel, Skeleton, StatCard, TimeSeries,
  fmtInt, fmtNum,
} from './primitives';

export function VitalsView() {
  const summary = useLiveResource(useCallback(() => dash.perfSummary(), []), 10000);
  const tokSeries = useLiveResource(
    useCallback(() => dash.perfSeries('tokens_per_sec', '1h', '24h', undefined, 'llm'), []), 15000);
  const cost = useLiveResource(useCallback(() => dash.perfCost('7d', 'app'), []), 30000);
  const hw = useLiveResource(useCallback(() => dash.hwHistory(), []), 5000);

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
  const latest = samples[samples.length - 1];
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

  return (
    <div className="db-view">
      <div className="db-view-head">
        <h2>Vitals</h2>
        <span className="db-view-sub">Ava's performance across all apps</span>
      </div>

      {/* KPI strip */}
      <div className="db-kpis">
        <StatCard label="Spend (7d)" tone="accent"
          value={cost.data ? `$${fmtNum(cost.data.spend_usd, 2)}` : '—'}
          hint={cost.data?.by ? `${Object.keys(cost.data.by).length} sources` : 'code API cost'} />
        <StatCard label="Energy (7d)" tone="accent"
          value={cost.data ? fmtNum(cost.data.energy_kwh, 3) : '—'} unit="kWh"
          hint={cost.data?.energy_usd != null ? `≈ $${fmtNum(cost.data.energy_usd, 2)}` : `~${fmtNum(cost.data?.avg_gpu_watts ?? null, 0)} W avg`} />
        <StatCard label="Throughput" value={fmtNum(tokAvg, 1)} unit="tok/s"
          tone={tokAvg == null ? 'default' : tokAvg < 15 ? 'warn' : 'ok'}
          hint={`${fmtInt(tokN)} completions`} />
        <StatCard label="TTFT" value={fmtNum(ttftAvg, 0)} unit="ms" hint="time to first token" />
        <StatCard label="Renders" value={fmtInt((s?.image?.count || 0) + (s?.video?.count || 0))}
          hint={`${fmtInt(s?.upscale?.count || 0)} upscales`} />
        <StatCard label="Route Errors" value={fmtInt(failovers)}
          tone={failovers ? 'warn' : 'ok'} hint="always-on model" />
      </div>

      {/* Row: inference throughput + model share */}
      <div className="db-grid db-grid-2">
        <Panel title="Inference throughput" subtitle="tokens/sec by model (24h)">
          {tokSeries.loading ? <Skeleton /> :
            tokSeries.data && tokSeries.data.points.length ?
              <TimeSeries points={tokSeries.data.points} series={tokSeries.data.series} unit="" /> :
              <EmptyState text="No inference recorded yet — chat with Ava to populate this." />}
        </Panel>
        <Panel title="Model routing" subtitle="share of completions served">
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

      {/* Hardware */}
      <Panel title="Hardware" subtitle="live device telemetry"
        right={latest ? <span className="db-panel-right-note">{latest.gpu_power != null ? `${Math.round(latest.gpu_power)} W` : ''}</span> : null}>
        <div className="db-gauges">
          <Gauge value={latest?.gpu_util ?? null} label="GPU util" />
          <Gauge value={latest?.gpu_temp ?? null} label="GPU temp" unit="°" max={100} warnAt={78} critAt={88} />
          <Gauge value={latest?.mem_used_pct ?? null} label="Memory" warnAt={85} critAt={95} />
          <Gauge value={latest?.cpu ?? null} label="CPU" />
        </div>
        {hwPoints.length > 1 ? (
          <div style={{ marginTop: 12 }}>
            <TimeSeries points={hwPoints} series={['GPU util', 'GPU temp', 'Mem %', 'CPU']} height={200} />
          </div>
        ) : <EmptyState text="Collecting hardware samples…" />}
      </Panel>
    </div>
  );
}
