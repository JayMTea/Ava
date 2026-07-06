// Chart theme — reads Ava's design tokens (tokens.css SSOT) at runtime so charts
// are always in lockstep with the app theme. No hard-coded hexes duplicated here.

export function token(name: string, fallback = ''): string {
  if (typeof window === 'undefined') return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

// Categorical cycle (accent-led), drawn from the token palette.
export function chartColors(): string[] {
  return [
    token('--chart-1', '#c96442'),
    token('--chart-2', '#3fb27f'),
    token('--chart-3', '#e0a93b'),
    token('--chart-4', '#7ea6c9'),
    token('--chart-5', '#b79cff'),
    token('--chart-6', '#9a968c'),
  ];
}

export const seriesColor = (i: number) => {
  const c = chartColors();
  return c[i % c.length];
};

export const semantic = () => ({
  ok: token('--ok', '#3fb27f'),
  warn: token('--warn', '#e0a93b'),
  err: token('--err', '#e0364d'),
  accent: token('--accent', '#c96442'),
  muted: token('--muted', '#9a968c'),
});

export const axisTick = () => ({ fill: token('--chart-axis', '#9a968c'), fontSize: 11 });
export const gridStroke = () => token('--chart-grid', '#3b3a36');

export function tooltipStyle(): React.CSSProperties {
  return {
    background: token('--panel', '#1f1e1d'),
    border: `1px solid ${token('--line', '#3b3a36')}`,
    borderRadius: 8,
    color: token('--txt', '#f4f3ee'),
    fontSize: 12,
  };
}

export const tooltipProps = () => ({
  contentStyle: tooltipStyle(),
  labelStyle: { color: token('--muted', '#9a968c') },
  itemStyle: { color: token('--txt', '#f4f3ee') },
});
