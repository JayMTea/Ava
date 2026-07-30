import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../../lib/icons';
import { api } from '../../lib/api';
import { MemoryPanel } from './MemoryPanel';
import { type TabId } from './shared';
import { useResource } from './hooks';
import { Overview } from './panels/Overview';
import { HardwarePanel } from './panels/HardwarePanel';
import { AgentPanel } from './panels/AgentPanel';
import { VoicePanel } from './panels/VoicePanel';
import { PersonaPanel } from './panels/PersonaPanel';
import { BudgetsPanel } from './panels/BudgetsPanel';
import { HistoryPanel } from './panels/HistoryPanel';
import { SystemPanel } from './panels/SystemPanel';
import { ConnectorsPanel } from './panels/ConnectorsPanel';
import { hub } from './hubApi';
import type { PendingApproval } from './hubApi';

// ─────────────────────────────────────────────────────────────────────────────
// Approvals banner — the agent parked a sensitive action; the operator decides.
// Polls so it appears on any Hub tab while a call is blocked waiting.
// ─────────────────────────────────────────────────────────────────────────────
function ApprovalsBanner() {
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
        <div key={p.id} className="hub-restart" style={{
          background: 'rgba(0,122,204,0.10)', color: 'var(--txt)',
          borderColor: 'color-mix(in srgb, var(--accent) 45%, transparent)',
          justifyContent: 'space-between', flexWrap: 'wrap', gap: 10,
        }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
            <span style={{ color: 'var(--accent)', display: 'inline-flex' }}><Icon name="lock" /></span>
            <span>
              <b>Approve action?</b> Ava wants to run <code>{p.action}</code> on <b>{p.connector}</b>
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
              <button className="hub-btn sm" onClick={() => decide(p.id, 'always')}
                title="Run it now and never ask again for this action — revoke anytime in the connector's settings">
                <Icon name="check" />Always allow</button>
            )}
            <button className={'hub-btn sm' + (p.grantable ? ' ghost' : '')} onClick={() => decide(p.id, 'approve')}>
              <Icon name="check" />{p.grantable ? 'Just once' : 'Approve'}</button>
            <button className="hub-btn ghost sm" onClick={() => decide(p.id, 'deny')}><Icon name="close" />Deny</button>
          </span>
        </div>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Shared bits
// ─────────────────────────────────────────────────────────────────────────────

const TABS: { id: TabId; label: string; icon: string }[] = [
  { id: 'overview', label: 'Overview', icon: 'gauge' },
  { id: 'hardware', label: 'Hardware', icon: 'chart' },
  { id: 'agent', label: 'Agent', icon: 'bot' },
  { id: 'connectors', label: 'Connectors', icon: 'panel' },
  { id: 'voice', label: 'Voice', icon: 'mic' },
  { id: 'persona', label: 'Persona', icon: 'bot' },
  { id: 'memory', label: 'Memory', icon: 'db' },
  { id: 'budgets', label: 'Budgets', icon: 'chart' },
  { id: 'history', label: 'History', icon: 'activity' },
  { id: 'system', label: 'System', icon: 'sliders' },
];

// The Hub sub-tab is kept in the URL hash as a second segment (#hub/<tab>) so a
// refresh or a bookmark lands back where you were. App.tsx's top-level router
// reads only the FIRST segment (`viewFromHash` does split('/')[0]), so this
// segment is invisible to it — no coupling, no fight over the hash.
const TAB_IDS = TABS.map((t) => t.id);

function tabFromHash(): TabId {
  if (typeof window === 'undefined') return 'overview';
  const parts = window.location.hash.replace(/^#\/?/, '').split('/');
  if (parts[0] !== 'hub') return 'overview';
  return (TAB_IDS as string[]).includes(parts[1]) ? (parts[1] as TabId) : 'overview';
}

function writeTabHash(t: TabId): void {
  // Overview is the default, so keep its URL clean as plain #hub.
  const next = t === 'overview' ? 'hub' : `hub/${t}`;
  if (window.location.hash.replace(/^#\/?/, '') !== next) window.location.hash = next;
}

// `docker` comes from /api/hub/system, which already reports the runtime — the
// banner used to say "restart Ava" and name no command, leaving the user to
// guess between a compose service and a bare process.
function RestartBanner({ show, docker }: { show: boolean; docker?: boolean }) {
  if (!show) return null;
  return (
    <div className="hub-restart">
      <Icon name="refresh" />
      <span>
        Saved to <b>ava.yaml</b>. Restart Ava to apply the change
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
  // Tab lives in the URL hash so it survives a refresh (#hub/<tab>).
  const [tab, setTabState] = useState<TabId>(() => tabFromHash());
  const setTab = useCallback((t: TabId) => { setTabState(t); writeTabHash(t); }, []);
  // Back/forward and manual hash edits move the tab too.
  useEffect(() => {
    const onHash = () => setTabState(tabFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const [restart, setRestart] = useState(false);
  const [brand, setBrand] = useState('Ava');
  useEffect(() => { api.brand().then((b) => b?.name && setBrand(b.name)).catch(() => {}); }, []);
  const notifyRestart = useCallback(() => setRestart(true), []);
  // Only used to name the right restart command in the banner. A failure here is
  // deliberately silent: the banner simply omits the command rather than
  // becoming an error the user cannot act on.
  const { data: sys } = useResource(() => hub.system());

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

        <ApprovalsBanner />
        <RestartBanner show={restart} docker={sys?.docker} />

        <div className="hub-tabs">
          {TABS.map((t) => (
            <button key={t.id} className={'hub-tab' + (tab === t.id ? ' active' : '')} onClick={() => setTab(t.id)}>
              <Icon name={t.icon} />{t.label}
            </button>
          ))}
        </div>

        {tab === 'overview' && <Overview onGo={setTab} />}
        {tab === 'hardware' && <HardwarePanel />}
        {tab === 'agent' && <AgentPanel onRestart={notifyRestart} />}
        {tab === 'connectors' && <ConnectorsPanel />}
        {tab === 'voice' && <VoicePanel onRestart={notifyRestart} />}
        {tab === 'persona' && <PersonaPanel onRestart={notifyRestart} />}
        {tab === 'memory' && <MemoryPanel />}
        {tab === 'budgets' && <BudgetsPanel />}
        {tab === 'history' && <HistoryPanel />}
        {tab === 'system' && <SystemPanel onRestart={notifyRestart} />}
      </div>
    </div>
  );
}
