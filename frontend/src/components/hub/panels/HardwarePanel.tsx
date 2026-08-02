import { EmptyState, Panel } from '../../dashboard/layout';
import { useResource } from '../hooks';
import { hub } from '../hubApi';
import { ResourceState } from '../ui/ResourceState';
import { Tile } from '../ui/Tile';
import { BudgetsPanel } from './BudgetsPanel';

// Hardware — detected compute + the recommended model tier it implies (the tier
// is the headline, since it drives which models Ava suggests under Agent), then
// the spend and energy caps. Budgets used to be its own tab; a cap reads better
// beside the machine that spends it than as a peer of Persona and Voice.
export function HardwarePanel() {
  const hwRes = useResource(() => hub.hardware());

  return (
    <>
    <Panel title="Your hardware"
      subtitle="Detected automatically — it sets the recommended model tier. Pick and download the model itself under Agent → Brain.">
      <ResourceState r={hwRes} label="your hardware"
        empty={<EmptyState text="Detecting hardware…" />}>
        {(hw) => (
        <>
          <div className="hw-hero">
            <Tile icon="chart" tone="accent" size={42} />
            <div className="hw-hero-body">
              <div className="hw-hero-tier">{hw.tier} tier</div>
              <div className="hw-hero-hint">{hw.hint}</div>
            </div>
          </div>
          <dl className="hub-kv" style={{ marginTop: 16 }}>
            <dt>Compute</dt><dd>{hw.gpu || 'No local GPU detected'}</dd>
            <dt>Usable memory</dt><dd>{hw.fit_gb != null ? `${hw.fit_gb} GB · ${hw.source || 'detected'}` : '—'}</dd>
          </dl>
          {hw.note && (
            <div className="hub-note" style={{ marginTop: 14 }}>{hw.note}</div>
          )}
          <div className="hub-note" style={{ marginTop: 14 }}>
            The tier sets which models Ava recommends. Pick and download one under
            <b> Agent → Brain</b>.
          </div>
        </>
        )}
      </ResourceState>
    </Panel>

    <div className="hub-section" />
    <BudgetsPanel />
    </>
  );
}
