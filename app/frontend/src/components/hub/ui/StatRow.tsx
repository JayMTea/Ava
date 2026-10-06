import type { ReactNode } from 'react';

// A status-board row: a tone dot + label + value, so a panel's checks scan
// top-to-bottom instead of as a mixed pile of badges and inline text. Shared by
// the Agent runtime and Voice gate panels.
export function StatRow({ label, value, tone }: {
  label: string; value: ReactNode; tone: 'ok' | 'warn' | 'err' | 'muted';
}) {
  return (
    <div className="stat-row">
      <span className={`stat-row-dot tone-${tone}`} aria-hidden="true" />
      <span className="stat-row-label">{label}</span>
      <span className="stat-row-val">{value}</span>
    </div>
  );
}
