import { EmptyState, Panel } from '../../dashboard/layout';
import { useResource } from '../hooks';
import { hub, type EngineLocality, type HardwareInfo } from '../hubApi';
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

// Where the engine is, in the owner's terms rather than the probe's. "host"
// carries the whole point — it is the one value that makes the memory reading on
// this page describe a different machine from the one the models run on.
const LOCALITY: Record<EngineLocality, string> = {
  container: 'inside Ava',
  compose: 'alongside Ava',
  host: 'on this machine, outside Ava',
  remote: 'over the network',
  unknown: 'location unknown',
};

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
  // The second fact this panel needs, and cannot answer alone: a tier measured
  // from Ava's container describes nothing at all when the engine turns out to
  // run on the machine outside it, with that machine's memory and GPU. Read
  // defensively — if the probe fails, the panel degrades to the hardware reading
  // it already had rather than to an error, because a hardware page that cannot
  // render because an ENGINE probe timed out is a worse answer than a partial one.
  const beRes = useResource(() => hub.backends());
  const engine = (beRes.data?.backends || []).find((b) => b.up) || null;
  // `|| engine` because a cached or version-skewed response predating
  // engine_label must degrade to the raw key, never to "undefined ·".
  const engineName = engine ? (engine.engine_label || engine.engine) : '';
  const elsewhere = !!engine && engine.locality === 'host' && !!beRes.data?.in_container;

  return (
    <>
    <Panel title="Your hardware"
      subtitle={elsewhere
        ? "What Ava can see from inside its container — which is not where your models run. See below."
        : "Detected automatically — it sets the recommended model tier. Pick and download the model itself under Agent → Brain."}>
      <ResourceState r={hwRes} label="your hardware"
        empty={<EmptyState text="Detecting hardware…" />}>
        {(hw) => (
        <>
          {/* Withhold the tier rather than publish one derived from the wrong
              machine. "Small tier" on a laptop whose native Ollama holds 32 GB
              and an RTX card is not a cautious answer, it is a wrong one. */}
          <div className="hw-hero">
            <Tile icon="chart" tone={elsewhere ? 'info' : 'accent'} size={42} />
            <div className="hw-hero-body">
              <div className={`hw-hero-tier${elsewhere ? ' as-written' : ''}`}>
                {elsewhere ? `${engineName} · on this machine, outside Ava` : `${hw.tier} tier`}
              </div>
              <div className="hw-hero-hint">
                {elsewhere
                  ? 'Ava thinks using an engine outside its container, so it does not size a tier from the reading below.'
                  : hw.hint}
              </div>
            </div>
          </div>
          <dl className="hub-kv" style={{ marginTop: 16 }}>
            <dt>Compute</dt><dd>{computeLabel(hw)}</dd>
            <dt>Usable memory</dt><dd>{hw.fit_gb != null ? `${hw.fit_gb} GB · ${hw.source || 'detected'}` : '—'}</dd>
            {engine && (
              <>
                <dt>Thinking with</dt>
                <dd>{engineName} · {LOCALITY[engine.locality]}<br />
                  <span className="hub-kv-sub">{engine.base_url}</span></dd>
              </>
            )}
          </dl>

          {elsewhere && (
            <div className="hub-note" style={{ marginTop: 14 }}>
              <b>The reading above is Ava's container, not the machine your models run on.</b>
              {' '}Ava reaches <b>{engineName}</b> at <code>{engine!.base_url}</code>,
              which is outside this container — it has the machine's own memory and
              graphics card, and Ava has no way to measure those from in here. So it
              reports what it can see and declines to recommend a size from it. Whatever
              that engine already holds is what will run; pick from it under
              <b> Agent → Brain</b>.
            </div>
          )}

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
              shipped compose file reserves one only for the inference service
              {elsewhere ? '' : ', so the tier above is sized from system memory'}.
              This limits what this page can report, not what your models can
              use. To read the card here too, drop a
              {' '}<code>deploy/docker-compose.override.yml</code> giving the
              {' '}<code>ava</code> service an NVIDIA <code>utility</code> device
              reservation — see “Surfacing the GPU in the bridge container” in
              {' '}<code>docs/INSTALL_REFERENCE.md</code>.
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

          {!elsewhere && (
            <div className="hub-note" style={{ marginTop: 14 }}>
              The tier sets which models Ava recommends. Pick and download one under
              <b> Agent → Brain</b>.
            </div>
          )}
        </>
        )}
      </ResourceState>
    </Panel>

    <div className="hub-section" />
    <BudgetsPanel />
    </>
  );
}
