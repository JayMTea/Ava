import { useEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Line, LineChart, Pie,
  PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { Icon } from '../../lib/icons';
import { axisTick, gridStroke, seriesColor, semantic, tooltipProps } from './chartTheme';
import { RANGES, type RangeKey } from './ranges';

/* ---------- InfoTip (hover/focus "what does this mean?" popover) ---------- */
// Renders into document.body so panel overflow can never clip it. Positioned
// against the trigger's viewport rect (fixed), above the icon. Works on hover,
// keyboard focus, and tap.
export function InfoTip({ text, label }: { text: string; label?: string }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState({ x: 0, y: 0 });
  const ref = useRef<HTMLButtonElement>(null);
  const show = () => {
    const r = ref.current?.getBoundingClientRect();
    if (r) setPos({ x: r.left + r.width / 2, y: r.top });
    setOpen(true);
  };
  const hide = () => setOpen(false);
  // Safety net while open: a hover popover must never wedge open if the
  // trigger's mouseleave is missed (seen under recording/load). Any pointer
  // activity away from the trigger, a scroll, or Escape dismisses it.
  useEffect(() => {
    if (!open) return;
    const offIfOutside = (e: Event) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onScroll = () => setOpen(false);
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('pointermove', offIfOutside, true);
    document.addEventListener('pointerdown', offIfOutside, true);
    document.addEventListener('scroll', onScroll, true);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointermove', offIfOutside, true);
      document.removeEventListener('pointerdown', offIfOutside, true);
      document.removeEventListener('scroll', onScroll, true);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);
  return (
    <>
      <button
        ref={ref}
        type="button"
        className="info-tip"
        aria-label={label ? `What is ${label}?` : 'What does this mean?'}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        onClick={(e) => { e.stopPropagation(); e.preventDefault(); open ? hide() : show(); }}
      >
        <Icon name="info" />
      </button>
      {open && createPortal(
        <div className="info-pop" role="tooltip" style={{ left: pos.x, top: pos.y }}>{text}</div>,
        document.body,
      )}
    </>
  );
}

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

/* ---------- StatusPill ---------- */
export function StatusPill({ status }: { status: string }) {
  const map: Record<string, string> = {
    up: 'ok', running: 'ok', active: 'ok', done: 'ok',
    down: 'err', error: 'err', failed: 'err',
    off: 'muted',  // feature turned off by the user — neutral, not an outage
    unknown: 'muted', queued: 'warn', warn: 'warn',
  };
  const tone = map[status] || 'muted';
  return <span className={`db-pill pill-${tone}`}><i className="db-dot" />{status}</span>;
}

/* ---------- TimeSeries (multi-line) ---------- */
// `xTickFmt`/`xTipFmt` let the caller adapt the axis to the selected range
// (clock for a day, weekday for a week, month/year for long ranges). Both take a
// unix-seconds timestamp; they default to a clock so short series still read well.
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
export function RangeSelector({
  value, onChange,
}: {
  value: RangeKey; onChange: (r: RangeKey) => void;
}) {
  return (
    <div className="db-seg db-range" role="tablist" aria-label="Time range">
      {RANGES.map((r) => (
        <button
          key={r.key}
          type="button"
          role="tab"
          aria-selected={value === r.key}
          className={'db-seg-btn' + (value === r.key ? ' on' : '')}
          onClick={() => onChange(r.key)}
        >
          {r.label}
        </button>
      ))}
    </div>
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
  value, label, unit = '%', max = 100, warnAt = 80, critAt = 92, help,
}: {
  value: number | null; label: string; unit?: string; max?: number;
  warnAt?: number; critAt?: number; help?: string;
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
      <div className="db-gauge-label">{label}{help && <InfoTip text={help} label={label} />}</div>
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
