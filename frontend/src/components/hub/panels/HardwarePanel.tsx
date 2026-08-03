import { EmptyState, Panel } from '../../dashboard/layout';
import { useResource } from '../hooks';
import { hub, type HardwareInfo } from '../hubApi';
import { ResourceState } from '../ui/ResourceState';
import { Tile } from '../ui/Tile';
import { BudgetsPanel } from './BudgetsPanel';

// Hardware — detected compute + the recommended model tier it implies (the tier
// is the headline, since it drives which models Ava suggests under Agent), then
// the spend and energy caps. Budgets used to be its own tab; a cap reads better
// beside the machine that spends it than as a peer of Persona and Voice.
//
// This panel is also where the machine detail LIVES. The first-run wizard shows
// a capability and folds the rest behind one disclosure, because a fault report
// on screen one of an install reads as a broken install — and the first outside
// tester to see the old version asked exactly that. Everything it declines to
// spell out is spelled out here, including the commands, so folding it away
// never costs anyone the answer.

// What the box is, said so that "we cannot see your card from inside a
// container" never renders as "you have no card". The old label was a bare
// `hw.gpu || 'No local GPU detected'`, which on the machine that prompted all
// this — an RTX A1000 laptop under Docker Desktop — claimed the absence of a
// card the user could see in Device Manager.
function computeLabel(hw: HardwareInfo): string {
  if (hw.pool_kind === 'vram') return `${hw.gpu || 'Graphics card'} · dedicated video memory`;
  if (hw.pool_kind === 'unified') return `${hw.gpu || 'Graphics'} · memory shared with the processor`;
  if (!hw.accel_measurable) return 'Processor · no graphics card readable from here';
  return hw.gpu || 'Processor · no graphics card detected';
}

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
            <dt>Compute</dt><dd>{computeLabel(hw)}</dd>
            <dt>Usable memory</dt><dd>{hw.fit_gb != null ? `${hw.fit_gb} GB · ${hw.source || 'detected'}` : '—'}</dd>
          </dl>

          {/* A pool that is a slice of a bigger machine is still the right number
              for anything running inside it — it just is not the machine's, and
              the gap between "my laptop has 32 GB" and "this says 15.4" is where
              a new installer decides the thing is broken. */}
          {hw.cap_kind === 'wsl2-vm' && (
            <div className="hub-note" style={{ marginTop: 14 }}>
              <b>{hw.fit_gb} GB is the container's share, not the machine's.</b> On
              Windows, Ava installs as a Linux container and Docker Desktop runs it
              on WSL2. Windows gives that VM about half the machine's memory by
              default, so this is what Ava can reach rather than what you have
              fitted. To raise it, put this in <code>%USERPROFILE%\.wslconfig</code>:
              <pre>{'[wsl2]\nmemory=24GB'}</pre>
              then run <code>wsl --shutdown</code> and start Docker Desktop again.
              Ava works either way — more memory simply moves it up a tier.
            </div>
          )}
          {hw.cap_kind === 'cgroup' && (
            <div className="hub-note" style={{ marginTop: 14 }}>
              <b>{hw.fit_gb} GB is this container's limit, not the machine's.</b> The
              host may have considerably more. Raise it with a <code>mem_limit</code>
              {' '}on the <code>ava</code> service in <code>deploy/docker-compose.yml</code>,
              or by lifting the limit on whatever runs the container.
            </div>
          )}

          {hw.note_code === 'container-no-gpu' && (
            <div className="hub-note" style={{ marginTop: 14 }}>
              <b>Your graphics card is fine — Ava just cannot see it from in here.</b>
              {' '}A container gets no GPU unless one is reserved for it, and the
              shipped compose file reserves one only for the inference service, so
              the tier above is sized from system memory. This limits what this
              page can report, not what your models can use. To read the card here
              too, give the <code>ava</code> service an NVIDIA <code>utility</code>
              {' '}device reservation — see “GPU telemetry in the bridge container”
              in <code>deploy/README.md</code>.
            </div>
          )}
          {hw.note_code === 'apple-silicon' && (
            <div className="hub-note" style={{ marginTop: 14 }}>
              <b>Apple Silicon shares one pool of memory</b> between the processor
              and the graphics, which is the figure above. Ava thinks using Ollama,
              MLX or LM Studio here — vLLM needs an NVIDIA card. GPU memory reads
              normally; utilisation, temperature and power come back blank, because
              macOS exposes no unprivileged API for them.
            </div>
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
