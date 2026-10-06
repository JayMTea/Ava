import type { CSSProperties } from 'react';
import { Icon } from '../../../lib/icons';

// The rounded, tinted identity glyph used everywhere in Setup — connector /
// memory / history rows, feature + governance rows, overview cards, the
// hardware hero. One primitive with a size and either a semantic `tone` or an
// explicit `color` (connectors pass their per-app accent), replacing seven
// near-identical `-ic` classes.
export type Tone = 'muted' | 'accent' | 'ok' | 'warn' | 'err' | 'info';

export function Tile({ icon, tone = 'muted', color, size = 30, className, style }: {
  icon: string;
  tone?: Tone;
  color?: string;      // explicit accent (overrides tone) — e.g. a connector's ui.color
  size?: number;
  className?: string;
  style?: CSSProperties;
}) {
  const colorStyle: CSSProperties | undefined = color
    ? { color, background: `color-mix(in srgb, ${color} 14%, transparent)` }
    : undefined;
  return (
    <span
      className={'tile' + (color ? '' : ` tone-${tone}`) + (className ? ' ' + className : '')}
      style={{ ['--tile-size' as string]: `${size}px`, ...colorStyle, ...style }}
      aria-hidden="true"
    >
      <Icon name={icon} />
    </span>
  );
}
