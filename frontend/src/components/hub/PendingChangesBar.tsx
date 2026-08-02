import { useEffect, useState } from 'react';
import { Icon } from '../../lib/icons';
import type { TabId } from './shared';
import { barView } from './provisionView';
import { startProvision, useProvisionState } from '../../hooks/useProvisionState';

/**
 * "You saved something; Ava hasn't got it yet" — and the one click that fixes it.
 *
 * This replaces a piece of homework. Saving a persona used to print
 * "Saved — re-provision the agent in Setup → Agent for it to take effect": a
 * navigation instruction, in a panel-local message that the next action wipes.
 * The owner had to know what "provision" meant, then go and do it.
 *
 * The component holds NO logic. Every decision is made by `barView()`, a pure
 * function covered exhaustively in provisionView.test.ts — which is what makes
 * this feature testable at all, given the SPA has no component-render harness.
 */
export function PendingChangesBar({ onGo }: { onGo?: (t: TabId) => void }) {
  const { state, job } = useProvisionState();
  const [busy, setBusy] = useState(false);
  const view = barView(state, job);

  // Falls back to the hash when there is no tab setter, so the bar can render
  // somewhere that does not own the Hub's tab state.
  //
  // Bare `hub/agent` is deliberate, not an unfinished address: it is the
  // canonical form of Agent → Runtime (hubRoute.ts keeps the default sub-tab
  // out of the URL), which is the sub-tab holding the Apply button this bar
  // sends you to. Spelling out `/runtime` would only get rewritten back.
  const go = () => {
    if (onGo) onGo('agent');
    else location.hash = 'hub/agent';
  };

  if (view.kind === 'hidden') return null;

  const apply = async () => {
    setBusy(true);
    await startProvision('all');
    setBusy(false);
  };

  // `role="status"` so a count change is announced without interrupting a screen
  // reader mid-sentence; only a failure is urgent enough for `alert`.
  const role = view.kind === 'failed' ? 'alert' : 'status';
  const tone = view.kind === 'failed' ? 'err' : view.kind === 'applied' ? 'ok' : 'accent';

  return (
    <div className={`hub-restart ${tone} spread`} role={role} aria-live="polite">
      {view.kind === 'pending' && (
        <>
          <span>
            <Icon name="info" />
            <b>{view.count === 1 ? '1 change is' : `${view.count} changes are`} waiting to
              reach Ava</b>{view.summary ? ` — ${view.summary}.` : '.'}
          </span>
          <span className="hub-btn-row">
            <button
              type="button" className="hub-btn" onClick={apply} disabled={busy}
              title="Push your saved persona, skills and policies into the agent sandbox. Takes a few seconds; chat keeps working."
            >
              <Icon name="check" />{busy ? 'Applying…' : 'Apply now'}
            </button>
            <button type="button" className="hub-btn ghost sm" onClick={go}
                    title="Open Setup → Agent to see exactly what changed.">
              Review
            </button>
          </span>
        </>
      )}

      {view.kind === 'applying' && (
        <>
          <span>
            <Icon name="refresh" />
            <b>Applying changes…</b>
            {view.observable && view.step ? ` ${view.step}` : ''}
            <Elapsed since={view.startedAt} />
          </span>
          <button type="button" className="hub-btn ghost sm" onClick={go}>View progress</button>
        </>
      )}

      {view.kind === 'applied' && (
        <span>
          <Icon name={view.partial ? 'info' : 'check'} />
          {view.partial ? (
            <>
              <b>Applied — but {view.summary} still isn’t live.</b>{' '}
              <button type="button" className="hub-btn ghost sm" onClick={go}>
                Open Agent setup
              </button>
            </>
          ) : (
            <b>Applied. Ava is using your latest setup.</b>
          )}
        </span>
      )}

      {view.kind === 'failed' && (
        <>
          <span><Icon name="close" /><b>Couldn’t apply your changes.</b> {view.detail}</span>
          <span className="hub-btn-row">
            <button type="button" className="hub-btn" onClick={apply} disabled={busy}>
              <Icon name="refresh" />Try again
            </button>
            <button type="button" className="hub-btn ghost sm" onClick={go}>See what failed</button>
          </span>
        </>
      )}

      {view.kind === 'agent-down' && (
        <>
          <span>
            <Icon name="info" />
            <b>{view.count === 1 ? '1 change is' : `${view.count} changes are`} waiting, but
              the agent isn’t running.</b> They’ll reach Ava the next time it starts.
          </span>
          <button type="button" className="hub-btn ghost sm" onClick={go}>Open Agent setup</button>
        </>
      )}
    </div>
  );
}

/** mm:ss since a unix timestamp. Freezes when `since` is null. */
function Elapsed({ since }: { since: number | null }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!since) return;
    const t = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(t);
  }, [since]);
  if (!since) return null;
  const s = Math.max(0, Math.floor(now - since));
  return <> · {Math.floor(s / 60)}:{String(s % 60).padStart(2, '0')}</>;
}
