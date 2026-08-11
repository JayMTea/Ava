import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../../lib/icons';
import { type TabId } from './shared';
import { DEFAULT_SUB, hubHash, parseHubHash } from './hubRoute';
import type { AgentSubTab, HubRoute } from './hubRoute';
import { useResource } from './hooks';
import { Overview } from './panels/Overview';
import { HardwarePanel } from './panels/HardwarePanel';
import { AgentPanel } from './panels/AgentPanel';
import { BrandingPanel } from './panels/BrandingPanel';
import { useBrandName } from '../../lib/brandContext';
import { SystemPanel } from './panels/SystemPanel';
import { ConnectorsPanel } from './panels/ConnectorsPanel';
import { hub } from './hubApi';
import type { PendingApproval } from './hubApi';
import { PendingChangesBar } from './PendingChangesBar';
import { agentSubPending, tabPending } from './provisionView';
import { useProvisionState } from '../../hooks/useProvisionState';
import { ViewErrorBoundary } from '../ViewErrorBoundary';

// ─────────────────────────────────────────────────────────────────────────────
// Approvals banner — the agent parked a sensitive action; the operator decides.
// Polls so it appears on any Hub tab while a call is blocked waiting.
// ─────────────────────────────────────────────────────────────────────────────
function ApprovalsBanner() {
  const brand = useBrandName();
  const [pending, setPending] = useState<PendingApproval[]>([]);
  useEffect(() => {
    let alive = true;
    const tick = () => hub.approvals().then((r) => { if (alive) setPending(r.pending); }).catch(() => {});
    tick();
    const t = setInterval(tick, 3000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  const decide = async (id: string, decision: 'approve' | 'always' | 'deny') => {
    setPending((p) => p.filter((x) => x.id !== id));
    try { await hub.decideApproval(id, decision); } catch { /* it may have timed out */ }
  };
  if (!pending.length) return null;
  return (
    <div style={{ marginBottom: 16 }}>
      {pending.map((p) => (
        <div key={p.id} className="hub-restart accent spread">
          <span style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
            <span style={{ color: 'var(--accent)', display: 'inline-flex' }}><Icon name="lock" /></span>
            <span>
              <b>Approve action?</b> {brand} wants to run <code>{p.action}</code> on <b>{p.connector}</b>
              {Object.keys(p.args).length > 0 && (
                <span style={{ color: 'var(--muted)' }}> · {Object.entries(p.args).map(([k, v]) => `${k}=${v}`).join(', ')}</span>
              )}
              {p.access === 'destructive' && (
                <span style={{ color: 'var(--muted)' }}> · destructive — asks every time</span>
              )}
              {p.access === 'physical' && (
                <span style={{ color: 'var(--muted)' }}> · physical action — moves something in the real world; asks every time</span>
              )}
            </span>
          </span>
          <span style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
            {p.grantable && (
              <button type="button" className="hub-btn sm" onClick={() => decide(p.id, 'always')}
                title="Run it now and never ask again for this action — revoke anytime in the connector's settings">
                <Icon name="check" />Always allow</button>
            )}
            <button type="button" className={'hub-btn sm' + (p.grantable ? ' ghost' : '')} onClick={() => decide(p.id, 'approve')}>
              <Icon name="check" />{p.grantable ? 'Just once' : 'Approve'}</button>
            <button type="button" className="hub-btn ghost sm" onClick={() => decide(p.id, 'deny')}><Icon name="close" />Deny</button>
          </span>
        </div>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Shared bits
// ─────────────────────────────────────────────────────────────────────────────

// Six tabs, down from eleven. Persona, Voice and Memory sat here as peers of
// Agent even though they ARE the agent — they are now its sub-tabs. Budgets
// merged into Hardware (a cap belongs beside the machine that spends it) and
// History moved to Data, which already rendered the same audit ledger.
const TABS: { id: TabId; label: string; icon: string }[] = [
  { id: 'overview', label: 'Overview', icon: 'gauge' },
  { id: 'hardware', label: 'Hardware', icon: 'chart' },
  { id: 'agent', label: 'Agent', icon: 'bot' },
  { id: 'connectors', label: 'Connectors', icon: 'panel' },
  { id: 'branding', label: 'Branding', icon: 'image' },
  { id: 'system', label: 'System', icon: 'sliders' },
];

/** Tab id -> the name its error boundary uses, so a crash says WHICH tab. */
const TAB_LABEL: Record<string, string> = Object.fromEntries(
  TABS.map((t) => [t.id, t.label]));

// The Hub sub-tab is kept in the URL hash as a second segment (#hub/<tab>), and
// Agent's own section as a third (#hub/agent/<sub>), so a refresh or a bookmark
// lands back where you were. App.tsx's top-level router reads only the FIRST
// segment (`viewFromHash` does split('/')[0]), so both are invisible to it — no
// coupling, no fight over the hash. The arithmetic itself lives in hubRoute.ts
// so vitest can cover the retired addresses; this file just applies it.
const TAB_IDS = TABS.map((t) => t.id);

// `docker` comes from /api/hub/system, which already reports the runtime — the
// banner used to say "restart Ava" and name no command, leaving the user to
// guess between a compose service and a bare process.
function RestartBanner({ show, docker }: { show: boolean; docker?: boolean }) {
  const brand = useBrandName();
  if (!show) return null;
  return (
    <div className="hub-restart">
      <Icon name="refresh" />
      <span>
        Saved to <b>ava.yaml</b>. Restart {brand} to apply the change
        {docker === undefined ? '' : ':'}
        {docker === true && <> <code>cd deploy &amp;&amp; docker compose restart ava</code></>}
        {docker === false && <> <code>./bin/ava up</code> (restart the service you started it with)</>}
      </span>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main
// ─────────────────────────────────────────────────────────────────────────────
export function HubView() {
  // Tab and Agent's sub-tab both live in the URL hash so they survive a refresh.
  const [route, setRoute] = useState<HubRoute>(() => parseHubHash(
    typeof window === 'undefined' ? '' : window.location.hash, TAB_IDS));
  const { tab, sub } = route;

  const go = useCallback((t: TabId, s: AgentSubTab = DEFAULT_SUB) => {
    const next = hubHash(t, s);
    setRoute({ tab: t, sub: s, canonical: next });
    if (window.location.hash.replace(/^#\/?/, '') !== next) window.location.hash = next;
  }, []);
  const setTab = useCallback((t: TabId) => go(t), [go]);
  const setSub = useCallback((s: AgentSubTab) => go('agent', s), [go]);

  // Back/forward and manual hash edits move the tab too.
  useEffect(() => {
    const onHash = () => {
      const r = parseHubHash(window.location.hash, TAB_IDS);
      // Someone navigated away from Setup entirely. We are still mounted for the
      // instant before App.tsx swaps the view, and this listener fires on that
      // same event — so touching the hash here would drag them straight back.
      if (r.foreign) return;
      // A tab that moved to another VIEW (History -> Data) is not ours to
      // resolve — segment 0 belongs to App.tsx. Hand it over and stop.
      if (r.leaveTo) { window.location.replace(`#${r.leaveTo}`); return; }
      setRoute(r);
      // A retired address (#hub/persona) or a junk sub-tab resolves to a
      // DIFFERENT url than the one in the bar. Rewrite with replaceState, never
      // by assigning location.hash: assigning pushes a history entry, so Back
      // would return to #hub/persona and redirect again — a Back button that
      // looks broken. replaceState fires no hashchange, so this cannot loop.
      if (window.location.hash.replace(/^#\/?/, '') !== r.canonical) {
        window.history.replaceState(null, '', `#${r.canonical}`);
      }
    };
    onHash();  // canonicalise the address we LOADED on, not just later ones
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const [restart, setRestart] = useState(false);
  // Was a SECOND /api/brand fetch, independent of App.tsx's, because passing the
  // name down as a prop through here to the panels was worse than duplicating
  // the request. The context makes that a false choice.
  const brand = useBrandName();
  const notifyRestart = useCallback(() => setRestart(true), []);
  // Only used to name the right restart command in the banner. A failure here is
  // deliberately silent: the banner simply omits the command rather than
  // becoming an error the user cannot act on.
  const { data: sys } = useResource(() => hub.system());
  // A tab badge counts only what THAT tab owns, so one edit does not light up
  // two tabs and read as two changes. Persona now lives inside Agent, so it
  // rolls up into the Agent badge and reappears split across the sub-tab bar —
  // the two must agree, and provisionView.test.ts asserts the sum. Suppressed
  // while a run is in flight: counts ticking down mid-deploy just flicker.
  const { state: prov, job: provJob } = useProvisionState();
  const running = provJob?.status === 'running';
  const tabBadges = running ? {} : tabPending(prov);
  const subBadges = running ? {} : agentSubPending(prov);

  return (
    <div className="hub view-scroll">
      <div className="hub-inner">
        <div className="hub-head" style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
          <div>
            <h2>Set up {brand}</h2>
            <p>Configure your hardware, agent, apps, and system — all from here, written to your config, nothing to source.</p>
          </div>
          <form method="post" action="/logout" style={{ flexShrink: 0 }}>
            <button type="submit" className="hub-btn ghost sm">
              <Icon name="lock" />Sign out
            </button>
          </form>
        </div>

        {/* Order is deliberate: an approval blocks a live, in-flight agent call
            with a human waiting on it. Pending changes block nothing. */}
        <ApprovalsBanner />
        <PendingChangesBar onGo={setTab} />
        <RestartBanner show={restart} docker={sys?.docker} />

        <div className="hub-tabs">
          {TABS.map((t) => {
            const n = tabBadges[t.id] ?? 0;
            return (
              <button
                type="button" key={t.id}
                className={'hub-tab' + (tab === t.id ? ' active' : '')}
                aria-label={n > 0
                  ? `${t.label} — ${n} change${n === 1 ? '' : 's'} waiting to reach Ava`
                  : undefined}
                onClick={() => setTab(t.id)}
              >
                <Icon name={t.icon} />{t.label}
                {/* The pill is decoration; the meaning is in the aria-label. */}
                {n > 0 && <span className="hub-tab-badge" aria-hidden="true">{n}</span>}
              </button>
            );
          })}
        </div>

        {/* One boundary PER TAB, not one around the view.
            App.tsx wraps all of Setup in a single ViewErrorBoundary, so any
            panel that threw took the whole thing down — the tab bar included —
            and Setup is the only place the owner can fix whatever caused it.
            That is not hypothetical: a `/api/hub/persona` answering 200 with a
            partial body crashed PersonaPanel on an unguarded `.map`, and the
            casualty was every other tab.

            Keyed on the tab so switching away and back RETRIES rather than
            leaving a dead panel behind — a boundary with no way out is its own
            trap. The outer boundary stays as the backstop for the shell. */}
        <ViewErrorBoundary key={tab} label={TAB_LABEL[tab] ?? 'This tab'}>
          {tab === 'overview' && <Overview onGo={setTab} />}
          {tab === 'hardware' && <HardwarePanel />}
          {tab === 'agent' && (
            <AgentPanel onRestart={notifyRestart} sub={sub} onSub={setSub} badges={subBadges} />
          )}
          {tab === 'connectors' && <ConnectorsPanel />}
          {tab === 'branding' && <BrandingPanel />}
          {tab === 'system' && <SystemPanel onRestart={notifyRestart} />}
        </ViewErrorBoundary>
      </div>
    </div>
  );
}
