/* Layout + formatting primitives for the dashboard and the Setup hub.
 *
 * Split out of the old primitives.tsx, which imported 14 recharts symbols in the
 * same module that exported Panel and EmptyState. Twelve files imported from it
 * and NONE of them drew a chart — so every Setup panel, the Data view and the
 * Memory panel pulled a 9.6 MB charting library into the same chunk, and no
 * amount of React.lazy could separate them while the barrel was shared.
 *
 * Rule for this file: nothing here may import recharts. Charts live in
 * ./charts.tsx, which imports FROM here.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { gridStroke, semantic } from './chartTheme';
import { Icon } from '../../lib/icons';
import { RANGES, type RangeKey } from './ranges';

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
  title, subtitle, right, children, className = '', pad = true, tour,
}: {
  title?: ReactNode; subtitle?: ReactNode; right?: ReactNode;
  children: ReactNode; className?: string; pad?: boolean;
  /** Stable anchor for the first-run walkthrough. A styling class would work
   *  until someone renamed it for styling reasons and silently unhooked a
   *  tour step; this says what it is for. */
  tour?: string;
}) {
  return (
    <section className={`db-panel ${className}`} data-tour={tour}>
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

/* ---------- RangeSelector (time window for a timeseries panel) ---------- */
// A dropdown, not a segmented control: six chips never fit beside a title in a
// half-width panel header, so the group wrapped onto a second row and read as
// broken. A native <select> also brings arrow keys, Home/End, type-ahead and the
// mobile wheel picker for free — where the old role="tablist" was decorative
// (no aria-controls, no roving tabindex, so arrows did nothing). Same choice the
// Data page's retention picker already made.
//
// No `appearance: none`: the native arrow and the native popup are what make it
// keyboard- and touch-correct, and tokens.css sets `color-scheme` per theme, so
// the browser paints the option list on-theme by itself.
//
// `label` names the chart, because two of these on one page must not both
// announce themselves as "Time range".
export function RangeSelector({
  value, onChange, label,
}: {
  value: RangeKey; onChange: (r: RangeKey) => void; label?: string;
}) {
  return (
    <select
      className="db-select"
      data-tour="range"
      aria-label={label ? `Time range — ${label}` : 'Time range'}
      value={value}
      onChange={(e) => onChange(e.target.value as RangeKey)}
    >
      {RANGES.map((r) => <option key={r.key} value={r.key}>{r.label}</option>)}
    </select>
  );
}

/* ---------- BarList (horizontal, labelled) ---------- */
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
