import { Fragment, type ReactNode } from 'react';
import { Icon } from '../../../lib/icons';

// A panel legend — the "what the actions do / how X works" term→description
// grid at the foot of Connectors, Memory, Budgets, and History. Was four
// copies of the same hand-written <dl>; now one component so every legend
// stays visually identical and a new one is a data array, not markup.
export interface LegendItem { icon: string; term: string; desc: ReactNode }

export function Legend({ title, items, foot }: {
  title: string; items: LegendItem[]; foot?: ReactNode;
}) {
  return (
    <div className="legend">
      <div className="legend-title">{title}</div>
      <dl className="legend-grid">
        {items.map((it) => (
          <Fragment key={it.term}>
            <dt className="legend-term"><Icon name={it.icon} />{it.term}</dt>
            <dd className="legend-desc">{it.desc}</dd>
          </Fragment>
        ))}
      </dl>
      {foot && <div className="legend-foot">{foot}</div>}
    </div>
  );
}
