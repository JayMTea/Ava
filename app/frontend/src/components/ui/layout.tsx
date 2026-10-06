/* Layout + formatting primitives shared by the Setup hub, the Agent console and
 * the Domains views.
 *
 * These were originally `components/dashboard/primitives.tsx`, which imported 14
 * recharts symbols in the same module that exported Panel and EmptyState — so
 * every Setup panel pulled a 9.6 MB charting library into its chunk. They were
 * split into dashboard/{layout,charts}.tsx to break that, and moved here when
 * the Vitals, Operations and Data views were removed: they outlived the
 * dashboard they were named for, and a `dashboard/` import in every Setup panel
 * described a page that no longer exists.
 *
 * Rule for this file, unchanged: nothing here may import a charting library.
 */

import { type ReactNode } from 'react';

/* ---------- formatting helpers ---------- */
export const fmtNum = (v: number | null | undefined, d = 0) =>
  v == null || Number.isNaN(v) ? '—' : v.toFixed(d);
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
// The `spark` and `help` props went with the dashboard: `spark` rendered a
// recharts Sparkline, which is the dependency this module exists to keep out,
// and `help` rendered an InfoTip whose glossary (dashboard/metrics.ts) was the
// Vitals/Operations metric list. Nothing passed either one.
export function StatCard({
  label, value, unit, hint, tone = 'default',
}: {
  label: string; value: ReactNode; unit?: string; hint?: ReactNode;
  tone?: 'default' | 'ok' | 'warn' | 'err' | 'accent';
}) {
  return (
    <div className={`db-stat tone-${tone}`}>
      <div className="db-stat-label">{label}</div>
      <div className="db-stat-value">
        {value}
        {unit && <span className="db-stat-unit">{unit}</span>}
      </div>
      {hint && <div className="db-stat-hint">{hint}</div>}
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
