// Shared time-range presets for every dashboard timeseries. One source of truth
// for: the label shown in the selector, the `since`/`bucket` the API is asked
// for, how often to poll (long ranges don't need 5s refresh), and how the x-axis
// ticks + tooltip label read for that range. Add a range here and it appears
// wherever <RangeSelector> + <TimeSeries> are used, with the axis already right.

export type RangeKey = 'day' | 'week' | 'month' | '3month' | 'year' | '5year';

export interface RangeDef {
  key: RangeKey;
  label: string;        // menu option + panel subtitle text
  since: string;        // API window, e.g. '7d' (m/h/d units)
  bucket: string;       // API aggregation step, e.g. '1h'
  pollMs: number;       // live-refresh cadence for this range
  tick: (ts: number) => string;  // x-axis tick label (ts = unix seconds)
  tip: (ts: number) => string;   // tooltip header (ts = unix seconds)
}

const d = (ts: number) => new Date(ts * 1000);
const fmt = (ts: number, o: Intl.DateTimeFormatOptions) => d(ts).toLocaleString([], o);

export const RANGES: RangeDef[] = [
  {
    key: 'day', label: 'Day', since: '1d', bucket: '5m', pollMs: 5000,
    tick: (t) => fmt(t, { hour: '2-digit', minute: '2-digit' }),
    tip: (t) => fmt(t, { weekday: 'short', hour: '2-digit', minute: '2-digit' }),
  },
  {
    key: 'week', label: 'Week', since: '7d', bucket: '1h', pollMs: 30000,
    tick: (t) => fmt(t, { weekday: 'short' }),
    tip: (t) => fmt(t, { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit' }),
  },
  {
    key: 'month', label: 'Month', since: '30d', bucket: '6h', pollMs: 60000,
    tick: (t) => fmt(t, { month: 'short', day: 'numeric' }),
    tip: (t) => fmt(t, { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit' }),
  },
  {
    key: '3month', label: '3 months', since: '90d', bucket: '1d', pollMs: 60000,
    tick: (t) => fmt(t, { month: 'short', day: 'numeric' }),
    tip: (t) => fmt(t, { month: 'short', day: 'numeric', year: 'numeric' }),
  },
  {
    key: 'year', label: '1 year', since: '365d', bucket: '1d', pollMs: 60000,
    tick: (t) => fmt(t, { month: 'short' }),
    tip: (t) => fmt(t, { month: 'short', day: 'numeric', year: 'numeric' }),
  },
  {
    key: '5year', label: '5 years', since: '1825d', bucket: '7d', pollMs: 60000,
    tick: (t) => fmt(t, { month: 'short', year: '2-digit' }),
    tip: (t) => fmt(t, { month: 'short', year: 'numeric' }),
  },
];

export const RANGE_MAP: Record<RangeKey, RangeDef> =
  Object.fromEntries(RANGES.map((r) => [r.key, r])) as Record<RangeKey, RangeDef>;
