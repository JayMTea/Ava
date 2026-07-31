import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../../lib/icons';
import { StatRow } from '../ui/StatRow';
import { DRIFT_LABEL, DRIFT_TONE } from '../provisionView';
import type { ProvisionJob, ProvisionScope, ProvisionState } from '../hubApi';

const SCOPE_LABEL: Record<string, string> = {
  persona: 'Persona',
  policies: 'App policies',
  servers: 'Tool servers',
  skills: 'Skills',
  runtime: 'Agent runtime',
};

/**
 * A live checklist for a provisioning run.
 *
 * Replaces a button label. `Provisioning…` on a disabled button was the entire
 * feedback for a call that could take ten minutes, and install.sh has a real hang
 * mode (§7's `recover` spawns a detached `ssh -f` that inherits stdout), so "slow"
 * and "wedged" looked identical.
 *
 * The log is collapsed by default — the checklist is the story, the log is the
 * evidence — and auto-expands on failure, which is the one time the evidence is
 * what you actually want.
 */
export function ProvisionRun({ job }: { job: ProvisionJob | null }) {
  const [showLog, setShowLog] = useState(false);
  const logRef = useRef<HTMLPreElement>(null);
  const running = job?.status === 'running';
  const failed = job?.status === 'error';

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [job?.log?.length]);

  if (!job || job.status === 'idle' || !job.id) return null;

  const steps = job.steps ?? [];

  return (
    <div className="hub-steps">
      <div className="hub-step">
        <span className={'hub-step-mark ' + (running ? 'run' : failed ? 'bad' : 'ok')}>
          <Icon name={running ? 'refresh' : failed ? 'close' : 'check'} />
        </span>
        <span style={{ minWidth: 0 }}>
          <b>
            {running ? 'Applying changes' : failed ? 'Couldn’t apply' : 'Applied'}
            <Elapsed from={job.started_at} to={job.ended_at} />
          </b>
          {job.detail && <div className="hub-step-detail">{job.detail}</div>}
        </span>
      </div>

      {/* The remote runtime has no streaming primitive: steps only arrive at the
          end. Drawing an empty checklist that never fills is a lie dressed as
          progress, so say what is actually true instead. */}
      {running && job.observable === false && (
        <div className="hub-step">
          <span className="hub-step-mark wait" />
          <span>This agent runs on another host and doesn’t report step-by-step
            progress. You’ll see the result when it finishes.</span>
        </div>
      )}

      {/* First frame: the job exists but nothing has reported yet. Render a row,
          never an empty box that flashes. */}
      {running && job.observable !== false && !steps.length && (
        <div className="hub-step">
          <span className="hub-step-mark run"><Icon name="refresh" /></span>
          <span>starting…</span>
        </div>
      )}

      {steps.map((s) => (
        <div className="hub-step" key={`${s.scope}/${s.id}`}>
          <span className={'hub-step-mark ' + (s.ok ? 'ok' : 'bad')}>
            <Icon name={s.ok ? 'check' : 'close'} />
          </span>
          <span style={{ minWidth: 0 }}>
            <b>{SCOPE_LABEL[s.scope] ?? s.scope} · {s.id}</b>
            {s.detail && <div className="hub-step-detail">{s.detail}</div>}
          </span>
        </div>
      ))}

      {!!job.log?.length && (
        <div className="hub-btn-row">
          <button type="button" className="hub-btn ghost sm"
                  onClick={() => setShowLog((v) => !v)}>
            {showLog || failed ? 'Hide log' : 'Show log'}
          </button>
        </div>
      )}
      {(showLog || failed) && !!job.log?.length && (
        <div className="hub-preview">
          <pre ref={logRef}>{job.log.join('\n')}</pre>
        </div>
      )}
    </div>
  );
}

/** mm:ss, frozen once the run ends — a clock that keeps ticking after the job
 *  finished is just wrong. */
function Elapsed({ from, to }: { from: number | null; to: number | null }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!from || to) return;
    const t = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(t);
  }, [from, to]);
  if (!from) return null;
  const s = Math.max(0, Math.floor((to ?? now) - from));
  return <> · {Math.floor(s / 60)}:{String(s % 60).padStart(2, '0')}</>;
}

/**
 * What the sandbox is actually running, per domain — the "Review" destination
 * the pending-changes bar links to. Built from StatRow, so no new primitives and
 * no new CSS.
 */
export function DriftBoard({ state }: { state: ProvisionState | null }) {
  if (!state || !state.enabled) return null;
  const order: ProvisionScope[] = ['persona', 'skills', 'policies', 'servers'];
  const label: Record<ProvisionScope, string> = {
    persona: 'Persona', skills: 'Skills', policies: 'App policies', servers: 'Tool servers',
  };
  return (
    <>
      <div className="stat-rows">
        {order.map((scope) => {
          const s = state.scopes?.[scope];
          if (!s) return null;
          const { stale, undeployed, unknown, total } = s.counts;
          const pending = stale + undeployed;
          const value = !total ? 'none'
            : unknown === total ? 'couldn’t be checked'
            : pending ? `${pending} of ${total} not applied`
            : `all ${total} live`;
          return (
            <StatRow key={scope} label={label[scope]} value={value}
                     tone={DRIFT_TONE[s.state]} />
          );
        })}
      </div>
      {!state.sandbox.live && state.sandbox.reason && (
        <div className="hub-note">
          <Icon name="info" />
          <span>{state.sandbox.reason} — so anything inside it shows as{' '}
            <i>{DRIFT_LABEL.unknown}</i> rather than as a change you need to make.</span>
        </div>
      )}
    </>
  );
}
