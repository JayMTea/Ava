import type { ReactNode } from 'react';
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Line, LineChart, Pie,
  PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { axisTick, gridStroke, seriesColor, semantic, tooltipProps } from './chartTheme';

/* ---------- formatting helpers ---------- */
export const fmtNum = (v: number | null | undefined, d = 0) =>
  v == null || Number.isNaN(v) ? '—' : v.toFixed(d);
export const fmtInt = (v: number | null | undefined) =>
  v == null ? '—' : Math.round(v).toLocaleString();
export const fmtTime = (ts?: number | null) =>
  ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—';
export const fmtClock = (ts?: number | null) =>
  ts ? new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
export const ago = (ts?: number | null) => {
  if (!ts) return '—';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

/* ---------- Panel (card) ---------- */
export function Panel({
  title, subtitle, right, children, className = '', pad = true,
}: {
  title?: ReactNode; subtitle?: ReactNode; right?: ReactNode;
  children: ReactNode; className?: string; pad?: boolean;
}) {
  return (
    <section className={`db-panel ${className}`}>
      {(title || right) && (
        <header className="db-panel-head">
          <div>
            {title && <h3 className="db-panel-title">{title}</h3>}
            {subtitle && <div className="db-panel-sub">{subtitle}</div>}
          </div>
          {right && <div className="db-panel-right">{right}</div>}
        </header>
      )}
      <div className={pad ? 'db-panel-body' : ''}>{children}</div>
    </section>
  );
}

/* ---------- StatCard ---------- */
export function StatCard({
  label, value, unit, hint, tone = 'default', spark,
}: {
  label: string; value: ReactNode; unit?: string; hint?: ReactNode;
  tone?: 'default' | 'ok' | 'warn' | 'err' | 'accent';
  spark?: number[];
}) {
  return (
    <div className={`db-stat tone-${tone}`}>
      <div className="db-stat-label">{label}</div>
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

/* ---------- StatusPill ---------- */
export function StatusPill({ status }: { status: string }) {
  const map: Record<string, string> = {
    up: 'ok', running: 'ok', active: 'ok', done: 'ok',
    down: 'err', error: 'err', failed: 'err',
    unknown: 'muted', queued: 'warn', warn: 'warn',
  };
  const tone = map[status] || 'muted';
  return <span className={`db-pill pill-${tone}`}><i className="db-dot" />{status}</span>;
}

/* ---------- TimeSeries (multi-line) ---------- */
export function TimeSeries({
  points, series, height = 220, xKey = 't', asArea = false, unit = '',
}: {
  points: Record<string, number>[]; series: string[]; height?: number;
  xKey?: string; asArea?: boolean; unit?: string;
}) {
  const xfmt = (t: number) =>
    new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const Chart = asArea ? AreaChart : LineChart;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <Chart data={points} margin={{ top: 8, right: 12, bottom: 0, left: -8 }}>
        <CartesianGrid stroke={gridStroke()} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey={xKey} tickFormatter={xfmt} tick={axisTick()} tickLine={false} axisLine={false} minTickGap={40} />
        <YAxis tick={axisTick()} tickLine={false} axisLine={false} width={44}
          tickFormatter={(v) => `${v}${unit}`} />
        <Tooltip {...tooltipProps()} labelFormatter={(t) => fmtClock(Number(t))} />
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

/* ---------- BarList (horizontal, labelled) ---------- */
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
export function Gauge({
  value, label, unit = '%', max = 100, warnAt = 80, critAt = 92,
}: {
  value: number | null; label: string; unit?: string; max?: number;
  warnAt?: number; critAt?: number;
}) {
  const v = value == null ? 0 : Math.max(0, Math.min(max, value));
  const pct = (v / max) * 100;
  const col = value == null ? semantic().muted
    : pct >= critAt ? semantic().err : pct >= warnAt ? semantic().warn : semantic().ok;
  const R = 34, C = Math.PI * R; // half circle
  const off = C * (1 - pct / 100);
  return (
    <div className="db-gauge">
      <svg viewBox="0 0 80 48" width="100%" height="72">
        <path d="M6 44 A34 34 0 0 1 74 44" fill="none" stroke={gridStroke()} strokeWidth="8" strokeLinecap="round" />
        <path d="M6 44 A34 34 0 0 1 74 44" fill="none" stroke={col} strokeWidth="8"
          strokeLinecap="round" strokeDasharray={C} strokeDashoffset={off} />
      </svg>
      <div className="db-gauge-val" style={{ color: col }}>
        {value == null ? '—' : Math.round(value)}<span>{unit}</span>
      </div>
      <div className="db-gauge-label">{label}</div>
    </div>
  );
}

/* ---------- EmptyState / Skeleton ---------- */
export function EmptyState({ text }: { text: string }) {
  return <div className="db-empty">{text}</div>;
}
export function Skeleton({ height = 200 }: { height?: number }) {
  return <div className="db-skeleton" style={{ height }} />;
}
