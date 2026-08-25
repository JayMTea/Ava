/** One domain's card. Presentational: props in, no fetching, so it can be
 *  rendered in a test over fixtures.
 *
 * The whole design problem here is that most of this data is ABSENT, and the
 * four kinds of absence are different facts: too thin to trust, could not be
 * read, nothing measures it, and never collected. Rendering is where those
 * collapse into one grey dash, so every branch carries a WORD, and every
 * explanation the backend wrote is rendered as text rather than hidden in a
 * tooltip.
 *
 * There is no cell-level score and no rollup across cells. The API cannot
 * produce one; neither may this.
 */
import { Icon } from '../../lib/icons';
import type { DomainCell, DomainSurface, PendingGrant, Provenance } from '../../lib/types';
import { fmtNum, Panel } from '../dashboard/layout';
import { Badge } from '../hub/ui/Badge';
import { Tile } from '../hub/ui/Tile';
import {
  allGaps, fmtDay, heroFor, nextAction, orderMetrics, STATE_COPY,shownState, 
  subtotalLine, unitLabel,
} from './cellView';

/** Three non-hue channels plus the word — deliberately not a `.tone-*`. A
 *  seventh tone would be a semantic claim the six-tone system does not make,
 *  and provenance is not a status. */
const PROV: Record<string, { glyph: string; word: string }> = {
  measured: { glyph: 'check', word: 'measured' },
  derived: { glyph: 'chart', word: 'derived' },
  assumed: { glyph: 'info', word: 'assumed' },
};

function ProvChip({ p }: { p: Provenance | null | undefined }) {
  // Empty when null. Inventing an "unknown" grade would add a claim.
  if (!p) return null;
  const d = PROV[p] ?? { glyph: 'info', word: p };
  return (
    <span className={`dm-prov dm-prov-${p}`}>
      <Icon name={d.glyph} /> {d.word}
    </span>
  );
}

export function CellCard({ cell, surfaces, pending, realmLabel }: {
  cell: DomainCell;
  surfaces: DomainSurface[];
  pending: PendingGrant[];
  realmLabel: string;
}) {
  if (!cell.ok) {
    return (
      <Panel title="Not found">
        <p className="dm-say">{cell.error || 'No such domain.'}</p>
        <a className="dm-action" href="#domains">Back to domains</a>
      </Panel>
    );
  }

  const hero = heroFor(cell.north_star);
  const rows = orderMetrics(cell.metrics ?? []);
  const gaps = allGaps(cell);
  const apps = (surfaces ?? [])
    .filter((s) => s.realm === cell.realm && s.domain === cell.domain)
    .map((s) => s.owner)
    .filter((x): x is string => !!x);
  const cov = cell.coverage ?? { metrics_ok: 0, metrics_declared: 0 } as DomainCell['coverage'];
  const declared = cov.metrics_declared ?? 0;
  const readOk = cov.metrics_ok ?? 0;

  return (
    <div className="dm-card">
      <div className="db-view-head">
        <h2>
          {realmLabel} · {cell.domain}
        </h2>
        {/* The floor is the WEAKEST evidence behind any number on this card, so
            an otherwise-measured card reads `assumed` the moment one metric is
            missing. That is the point, not a bug. */}
        <ProvChip p={cell.provenance_floor} />
      </div>

      <Panel title="North star" subtitle={cell.tree?.cadence ? `reviewed ${cell.tree.cadence}` : undefined}>
        {hero.kind === 'figure' ? (
          <>
            <div className="dm-hero">{fmtNum(hero.obs.value)}</div>
            <div className="dm-meta">
              <span className="dm-metric">{hero.obs.metric}</span>
              {hero.obs.unit ? <span>{unitLabel(hero.obs.unit)}</span> : null}
              <ProvChip p={hero.obs.provenance} />
              {hero.obs.n != null ? <span>n={hero.obs.n}</span> : null}
              {hero.obs.lo != null ? (
                <span>range {fmtNum(hero.obs.lo)}–{fmtNum(hero.obs.hi ?? null)}</span>
              ) : null}
              <span>as of {fmtDay(hero.obs.day)}</span>
            </div>
          </>
        ) : (
          <div className="dm-hero-say">
            <Tile icon={hero.glyph} tone={hero.tone === 'warn' ? 'warn' : 'muted'} size={28} />
            <div>
              <p className="dm-say">{hero.text}</p>
              {hero.why ? <p className="dm-why">{hero.why}</p> : null}
            </div>
          </div>
        )}
        {cell.north_star?.by_dim ? (
          <table className="db-table dm-dims">
            <tbody>
              {shownState(cell.north_star).dims.map((d) => (
                <tr key={d.dim}>
                  <td>{d.dim}</td>
                  <td className="dm-num">{d.value == null ? '—' : fmtNum(d.value)}</td>
                  <td><Badge tone={d.state === 'unavailable' ? 'warn' : 'muted'}>
                    {STATE_COPY[d.state].label}</Badge></td>
                  <td><ProvChip p={d.provenance} /></td>
                  <td className="dm-why">{d.why}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Panel>

      <Panel title="Metrics"
             subtitle={`${readOk} of ${declared} with a reading`}>
        {/* Neither number is a verdict — the ratio in words carries the meaning.
            Collector coverage in DAYS is estate-wide and is stated once on the
            index, never here where it would wear this domain's name. */}
        <div className="dm-meter" aria-hidden="true">
          <span style={{ width: declared ? `${(readOk / declared) * 100}%` : '0%' }} />
        </div>
        <div className="db-table-wrap">
          <table className="db-table">
            <thead>
              <tr>
                <th>Metric</th><th>Value</th><th>State</th>
                <th>Provenance</th><th>As of</th><th>Next</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((o) => {
                const s = shownState(o);
                const act = nextAction(o, pending);
                return (
                  <tr key={o.metric}>
                    <td>
                      <div className="dm-metric">{o.metric}</div>
                      {o.why ? <div className="dm-why">{o.why}</div> : null}
                    </td>
                    <td className="dm-num">
                      {o.state === 'ok' && o.value != null
                        ? `${fmtNum(o.value)} ${unitLabel(o.unit)}`.trim()
                        : '—'}
                    </td>
                    <td>
                      <Badge tone={s.state === 'unavailable' ? 'warn' : 'muted'}>{s.label}</Badge>
                      {s.detail ? <div className="dm-why">{s.detail}</div> : null}
                    </td>
                    <td><ProvChip p={s.provenance} /></td>
                    <td className="dm-num">
                      {fmtDay(o.day)}
                      {o.n != null ? <div className="dm-why">n={o.n}</div> : null}
                    </td>
                    <td>
                      {act.href
                        ? <a className="dm-action" href={act.href}>{act.text}</a>
                        : <span className="dm-why">{act.text}</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>

      {(cell.subtotals ?? []).length > 0 && (
        <Panel title="Subtotals">
          {/* Never "Total", and no line ever adds the lines. The backend groups
              by unit STRING, and sharing a unit is not being commensurable — so
              the contributors are named, which makes a meaningless sum obvious
              to whoever declared it. */}
          {cell.subtotals.map((s) => {
            const line = subtotalLine(s, cell.metrics ?? []);
            return (
              <div className="dm-sub" key={s.unit}>
                <div className="dm-sub-head">
                  <span>Subtotal · {unitLabel(s.unit)}</span>
                  <span className="dm-num">{line.value == null ? '—' : fmtNum(line.value)}</span>
                </div>
                <div className="dm-why">
                  {line.sums.length ? `sums ${line.sums.join(', ')}` : 'nothing to sum'}
                </div>
                {line.missing.map((m) => (
                  <div className="dm-why" key={m.metric}>left out {m.metric} — {m.why}</div>
                ))}
              </div>
            );
          })}
        </Panel>
      )}

      {gaps.length > 0 && (
        <Panel title="What is missing" subtitle={`${gaps.length}`}>
          {gaps.map((g) => (
            <div className="dm-gap" key={g.metric}>
              <Badge tone={g.state === 'unavailable' ? 'warn' : 'muted'}>
                {STATE_COPY[g.state].label}
              </Badge>
              <span className="dm-metric">{g.metric}</span>
              <span className="dm-why">{g.why}</span>
            </div>
          ))}
        </Panel>
      )}

      {(cell.tree?.unresolved ?? []).length > 0 && (
        <Panel title="Declared but unresolved">
          {(cell.tree.unresolved ?? []).map((m) => (
            <div className="dm-gap" key={m}>
              <Badge tone="err">Unresolved</Badge>
              <span className="dm-metric">{m}</span>
            </div>
          ))}
        </Panel>
      )}

      {apps.length > 0 && (
        <p className="dm-why">Fed by {Array.from(new Set(apps)).join(', ')}.</p>
      )}
    </div>
  );
}
