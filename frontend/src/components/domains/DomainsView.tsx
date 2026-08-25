/** The Domains view: the cells that exist, and one card when a cell is chosen.
 *
 * There is deliberately NO number here that spans cells. No estate score, no
 * "percent healthy", no count of good domains. The API cannot produce one —
 * that absence is the enforcement — and a UI that computed one anyway would
 * reintroduce exactly the composite this layer refuses.
 */
import { useEffect, useState } from 'react';
import { api } from '../../lib/api';
import { Icon } from '../../lib/icons';
import { realmLabel as labelOf, railRealms } from '../../lib/realms';
import type { DomainsCatalogue } from '../../lib/types';
import { Panel } from '../dashboard/layout';
import { useResource } from '../hub/hooks';
import { Badge } from '../hub/ui/Badge';
import { ResourceState } from '../hub/ui/ResourceState';
import { CellCard } from './CellCard';
import { focusFromHash, hashForCell } from './domainsRoute';

function Index({ cat }: { cat: DomainsCatalogue }) {
  const axis = railRealms(cat).axis;
  const cov = cat.coverage;
  return (
    <>
      {cov && cov.days_expected > 0 && (
        // Stated ONCE, here. `kpi_store.coverage()` takes no realm or domain —
        // it reads one heartbeat ledger and answers "days the collector ran",
        // which is an estate fact. Repeating it on every card would stamp one
        // global number with nine different domains' names.
        <div className="db-banner db-banner-note">
          Collected on {cov.days_collected} of the last {cov.days_expected} days.
          {cov.days_collected < cov.days_expected
            ? ' A day with no run is a gap, not a zero.' : ''}
        </div>
      )}

      {cat.problems.length > 0 && (
        <Panel title="Problems in the catalogue">
          {cat.problems.map((p) => (
            <div className="dm-gap" key={p}>
              <Badge tone="warn">Declaration</Badge>
              <span className="dm-why">{p}</span>
            </div>
          ))}
        </Panel>
      )}

      {cat.pending_grants.length > 0 && (
        <Panel title="Waiting on your permission"
               subtitle={`${cat.pending_grants.length}`}>
          <p className="dm-why">
            These reads disclose something, so they are refused until you allow
            each one. Nothing is collected from them meanwhile.
          </p>
          {cat.pending_grants.map((g) => (
            <div className="dm-gap" key={`${g.connector}.${g.tool}`}>
              <Badge tone="muted">{g.tier}</Badge>
              <span className="dm-metric">{g.connector} · {g.tool}</span>
              <span className="dm-why">{g.metrics.length} metric(s)</span>
            </div>
          ))}
        </Panel>
      )}

      <Panel title="Domains" subtitle={`${cat.cells.length}`}>
        <div className="dm-cells">
          {cat.cells.map((c) => {
            const surfaces = cat.surfaces.filter(
              (s) => s.realm === c.realm && s.domain === c.domain);
            const metrics = surfaces.reduce((n, s) => n + (s.metrics || 0), 0);
            return (
              <a className="dm-cell" key={`${c.realm}/${c.domain}`}
                 href={hashForCell(c.realm, c.domain)}>
                <span className="dm-cell-realm">{labelOf(axis, c.realm)}</span>
                <span className="dm-cell-domain">{c.domain}</span>
                {/* Counts of DECLARED things only. Anything about how those
                    metrics are doing needs the cell's own payload, and
                    fetching nine of them here would be the first step back
                    toward the rollup the API exists to make unbuildable. */}
                <span className="dm-why">
                  {surfaces.length} surface(s) · {metrics} metric(s)
                </span>
                <Icon name="arrowRight" />
              </a>
            );
          })}
        </div>
      </Panel>
    </>
  );
}

function Cell({ cat, realm, domain }: {
  cat: DomainsCatalogue; realm: string; domain: string;
}) {
  const r = useResource(() => api.domainCell(realm, domain), [realm, domain]);
  const axis = railRealms(cat).axis;
  return (
    <ResourceState r={r} label="this domain">
      {(cell) => (
        <CellCard cell={cell} surfaces={cat.surfaces}
                  pending={cat.pending_grants} realmLabel={labelOf(axis, realm)} />
      )}
    </ResourceState>
  );
}

export default function DomainsView() {
  const cat = useResource(() => api.domains(), []);
  // App.tsx owns hash segment 0; this owns the rest. One listener, and the
  // parsing lives in a pure module beside it.
  const [focus, setFocus] = useState(() => focusFromHash(window.location.hash));
  useEffect(() => {
    const onHash = () => setFocus(focusFromHash(window.location.hash));
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  return (
    <div className="db-view dm-view">
      <ResourceState r={cat} label="your domains">
        {(c) => {
          if (c.enabled === false) {
            return (
              <Panel title="Domains">
                <p className="dm-say">Domains is off.</p>
                <p className="dm-why">
                  Turn on <b>features.domains</b> to group your apps into your own
                  domains and collect a metric series for each one.
                </p>
              </Panel>
            );
          }
          if (focus.realm && focus.domain) {
            return (
              <>
                <a className="dm-action" href="#domains">← All domains</a>
                <Cell cat={c} realm={focus.realm} domain={focus.domain} />
              </>
            );
          }
          return <Index cat={c} />;
        }}
      </ResourceState>
    </div>
  );
}
