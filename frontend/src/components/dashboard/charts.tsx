/* Chart primitives — the only module that imports recharts.
 *
 * Kept apart from ./layout.tsx so that importing Panel does not drag a charting
 * library along with it. Only the two chart-bearing views (VitalsView, OpsView)
 * import this, and App.tsx loads them lazily, so a chat-only session never
 * downloads recharts at all.
 */

import { type ReactNode } from 'react';
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Line, LineChart, Pie,
  PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { axisTick, gridStroke, seriesColor, semantic, tooltipProps } from './chartTheme';
import { InfoTip, fmtClock } from './layout';

export function StatCard({
  label, value, unit, hint, tone = 'default', spark, help,
}: {
  label: string; value: ReactNode; unit?: string; hint?: ReactNode;
  tone?: 'default' | 'ok' | 'warn' | 'err' | 'accent';
  spark?: number[]; help?: string;
}) {
  return (
    <div className={`db-stat tone-${tone}`}>
      <div className="db-stat-label">{label}{help && <InfoTip text={help} label={label} />}</div>
      <div className="db-stat-value">
        {value}
        {unit && <span className="db-stat-unit">{unit}</span>}
      </div>
      {hint && <div className="db-stat-hint">{hint}</div>}
      {spark && spark.length > 1 && <Sparkline data={spark} />}
    </div>
  );
}

/* ---------- Sparkline ---------- */
export function Sparkline({ data, height = 30 }: { data: number[]; height?: number }) {
  const rows = data.map((v, i) => ({ i, v }));
  return (
    <div className="db-spark" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={rows} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={semantic().accent} stopOpacity={0.35} />
              <stop offset="100%" stopColor={semantic().accent} stopOpacity={0} />
            </linearGradient>
          </defs>
          <Area type="monotone" dataKey="v" stroke={semantic().accent}
            strokeWidth={2} fill="url(#sparkFill)" isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

/* ---------- TimeSeries ---------- */
export function TimeSeries({
  points, series, height = 220, xKey = 't', asArea = false, unit = '',
  xTickFmt, xTipFmt,
}: {
  points: Record<string, number>[]; series: string[]; height?: number;
  xKey?: string; asArea?: boolean; unit?: string;
  xTickFmt?: (t: number) => string; xTipFmt?: (t: number) => string;
}) {
  const xfmt = xTickFmt ?? ((t: number) =>
    new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }));
  const tipFmt = xTipFmt ?? ((t: number) => fmtClock(t));
  const Chart = asArea ? AreaChart : LineChart;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <Chart data={points} margin={{ top: 8, right: 12, bottom: 0, left: -8 }}>
        <CartesianGrid stroke={gridStroke()} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey={xKey} tickFormatter={xfmt} tick={axisTick()} tickLine={false} axisLine={false} minTickGap={40} />
        <YAxis tick={axisTick()} tickLine={false} axisLine={false} width={44}
          tickFormatter={(v) => `${v}${unit}`} />
        <Tooltip {...tooltipProps()} labelFormatter={(t) => tipFmt(Number(t))} />
        {series.map((s, i) =>
          asArea ? (
            <Area key={s} type="monotone" dataKey={s} stroke={seriesColor(i)}
              fill={seriesColor(i)} fillOpacity={0.15} strokeWidth={2.5} isAnimationActive={false} dot={false} />
          ) : (
            <Line key={s} type="monotone" dataKey={s} stroke={seriesColor(i)}
              strokeWidth={2.5} dot={false} isAnimationActive={false} connectNulls />
          ),
        )}
      </Chart>
    </ResponsiveContainer>
  );
}

/* ---------- RangeSelector (Day / Week / … / 5Y) ---------- */
// Drop-in segmented control for any timeseries. Reuses the .db-seg styles.
export function BarList({
  data, height = 220, unit = '',
}: {
  data: { name: string; value: number }[]; height?: number; unit?: string;
}) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 8 }}>
        <CartesianGrid stroke={gridStroke()} strokeDasharray="3 3" horizontal={false} />
        <XAxis type="number" tick={axisTick()} tickLine={false} axisLine={false} tickFormatter={(v) => `${v}${unit}`} />
        <YAxis type="category" dataKey="name" tick={axisTick()} tickLine={false} axisLine={false} width={120} />
        <Tooltip {...tooltipProps()} cursor={{ fill: 'rgba(255,255,255,0.04)' }} />
        <Bar dataKey="value" radius={[0, 6, 6, 0]}>
          {data.map((_, i) => <Cell key={i} fill={seriesColor(i)} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/* ---------- Donut ---------- */
export function Donut({
  data, height = 200,
}: {
  data: { name: string; value: number }[]; height?: number;
}) {
  const total = data.reduce((a, b) => a + b.value, 0);
  return (
    <div className="db-donut">
      <ResponsiveContainer width="100%" height={height}>
        <PieChart>
          <Pie data={data} dataKey="value" nameKey="name" innerRadius="60%" outerRadius="85%"
            paddingAngle={2} isAnimationActive={false} stroke="none">
            {data.map((_, i) => <Cell key={i} fill={seriesColor(i)} />)}
          </Pie>
          <Tooltip {...tooltipProps()} />
        </PieChart>
      </ResponsiveContainer>
      <div className="db-donut-legend">
        {data.map((d, i) => (
          <div key={d.name} className="db-legend-row">
            <i className="db-legend-dot" style={{ background: seriesColor(i) }} />
            <span className="db-legend-name">{d.name}</span>
            <span className="db-legend-val">{total ? Math.round((100 * d.value) / total) : 0}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---------- Gauge (radial utilisation dial) ---------- */
