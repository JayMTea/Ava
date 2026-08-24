import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChatMessage, Session } from '../../lib/agentApi';
import { agentApi } from '../../lib/agentApi';
import { api } from '../../lib/api';
import type { ChatSummary } from '../../lib/types';
import { useGateway, useGatewaySubscription } from '../../hooks/useGateway';

import { ViewErrorBoundary } from '../ViewErrorBoundary';
import { GatewayStatusChip } from './GatewayStatusChip';
import { ActivityList } from './ActivityList';
import { AutomationsList } from './AutomationsList';
import { RunInspector } from './RunInspector';
import { SessionList } from './SessionList';
import { SidePanel } from './SidePanel';
import { Thread } from './Thread';
import {
  AGENT_SECTIONS, agentHash, type AgentRoute, type AgentSection,
  parseAgentHash,
} from './agentRoute';
import {
  chatIdOfSession, DEFAULT_SESSION_PREFIX, groupConsoleSessions,
} from './agentView';

// The agent console. Sessions · Activity · Automations, and nothing else.
//
// WHY THREE SECTIONS AND NOT FIFTEEN
// ----------------------------------
// The upstream Control UI has roughly fifteen screens, and about half of them
// are CONFIGURATION — models and providers, secrets, MCP servers, devices,
// plugins, voice. Ava already owns a home for every one of those
// (Setup → Agent → Brain, Setup → Connectors, Setup → Agent → Voice…), and
// CLAUDE.md is explicit: "A surface has exactly one home. When two places would
// show the same thing, one owns it and the other links to it."
//
// So the rule here is: **this tab owns everything about a LIVE SESSION.
// Anything that outlives a session goes to the Setup home that already owns
// that job.** Still rebuilt natively — just not all in one tab. That is the
// difference between a product and a second product bolted onto the first.
//
// There is deliberately no fourth "Overview / Health" section. A fresh install
// needs somewhere that says "here is your gateway, here is what is wrong" —
// and that place is the console's EMPTY STATE, which is where you already are.
// A route for it is a page you visit once and a tab that reads as dead weight
// forever after. The status chip in the section bar covers the glance; Setup →
// Agent → Runtime stays the authoritative page and the chip links there.
//
// FULL BLEED, NOT A HUB PANEL. `styles/hub.css` caps `.hub-inner` at 920px,
// which is right for a reading column and wrong for a session list beside a
// thread beside a side panel. This reuses Hub's ATOMS (Panel, Tile, Badge,
// StatRow, the .tone-* system) and none of its column.

export function AgentView({ active = true }: { active?: boolean }) {
  const [route, setRoute] = useState<AgentRoute>(() => parseAgentHash(
    typeof window === 'undefined' ? '' : window.location.hash));

  const go = useCallback((next: Partial<AgentRoute>) => {
    const hash = agentHash({ ...next });
    setRoute(parseAgentHash(`#${hash}`));
    // Assign (not replaceState) for USER navigation, so Back walks the sessions
    // they actually visited.
    if (window.location.hash.replace(/^#\/?/, '') !== hash) {
      window.location.hash = hash;
    }
  }, []);

  // The ONE applier for everything below segment 0, mirroring HubView.
  useEffect(() => {
    const onHash = () => {
      const r = parseAgentHash(window.location.hash);
      // This view is KEPT ALIVE after the user leaves (see App.tsx — unmounting
      // would kill the terminal and the browser panel), so `foreign` matters
      // more here than in Setup: without it, every hashchange anywhere in the
      // app would drag the user back to #agent.
      if (r.foreign) return;
      setRoute(r);
      // replaceState, never `location.hash =`. Assigning pushes a history entry,
      // so Back would return to the non-canonical address and redirect again —
      // a Back button that looks broken. replaceState fires no hashchange, so
      // this cannot loop.
      if (window.location.hash.replace(/^#\/?/, '') !== r.canonical) {
        window.history.replaceState(null, '', `#${r.canonical}`);
      }
    };
    onHash();   // canonicalise the address we LOADED on, not only later ones
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const setSection = (id: AgentSection) => go({ section: id });

  return (
    <div className="agent view-scroll" data-active={active ? '1' : '0'}>
      <div className="agent-bar">
        <nav className="hub-tabs" aria-label="Agent sections">
          {AGENT_SECTIONS.map((s) => (
            <button
              type="button"
              key={s.id}
              className={`hub-tab${route.section === s.id ? ' on' : ''}`}
              aria-current={route.section === s.id ? 'page' : undefined}
              onClick={() => setSection(s.id)}
            >
              {s.label}
            </button>
          ))}
        </nav>
        <GatewayStatusChip />
      </div>

      {/* One boundary PER SECTION, keyed on the section. The HubView lesson: a
          crash in Activity must not take the console down with it, and moving
          away must clear the error rather than stranding it. */}
      <ViewErrorBoundary key={route.section} label={`Agent — ${route.section}`}>
        {route.section === 'sessions' && <SessionsSection route={route} onGo={go} />}
        {route.section === 'activity' && <ActivitySection route={route} onGo={go} />}
        {route.section === 'automations' && <AutomationsSection route={route} onGo={go} />}
      </ViewErrorBoundary>
    </div>
  );
}

type SectionProps = { route: AgentRoute; onGo: (r: Partial<AgentRoute>) => void };

function SessionsSection({ route, onGo }: SectionProps) {
  const client = useGateway();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(true);
  // What names the chat-origin rows: the chat list (real titles) and the
  // session-key prefix. The prefix is env-configurable (AVA_OC_SESSION) and a
  // SERVER fact — /api/gateway/status reports it as `session_prefix` — so it
  // is fetched, with the stock default standing in until (or unless) the
  // bridge answers.
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [prefix, setPrefix] = useState(DEFAULT_SESSION_PREFIX);

  useEffect(() => {
    let live = true;
    void fetch('/api/gateway/status', { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (live && typeof j?.session_prefix === 'string' && j.session_prefix) {
          setPrefix(j.session_prefix);
        }
      })
      .catch(() => { /* the default stands; labels degrade, nothing breaks */ });
    return () => { live = false; };
  }, []);

  const load = useCallback(async () => {
    if (!client) return;
    // The chat list rides along with every (re)load so renames made in Chats
    // reach the rows here. Its failure only downgrades labels to cids — it
    // must never take the session list down with it, so it is not awaited
    // inside the try below.
    void api.listChats().then((r) => setChats(r.chats || [])).catch(() => {});
    try {
      const got = await agentApi(client).sessions.list();
      setSessions(got?.sessions || []);
      setErr('');
    } catch (e) {
      // Surfaced, never swallowed: a console that renders an empty list when
      // the gateway is unreachable tells the owner they have no sessions.
      setErr((e as Error).message || String(e));
    } finally {
      setLoading(false);
    }
  }, [client]);

  useEffect(() => { void load(); }, [load]);

  // Keep the list live off the RUN LIFECYCLE. There is no `session.update`
  // topic — the gateway's whole event vocabulary is agent/chat/health/tick
  // (verified live), so the old subscription never fired and the list only
  // refreshed on mount. A run's `agent` frames DO carry lifecycle
  // (stream=lifecycle, data.phase start|end), so a start or end means a row
  // changed. `agent` also fires per assistant token, so filter on the stream
  // and debounce — one run emits start + N deltas + end, and we want one reload.
  const reloadTimer = useRef<number | undefined>(undefined);
  const scheduleReload = useCallback(() => {
    window.clearTimeout(reloadTimer.current);
    reloadTimer.current = window.setTimeout(() => { void load(); }, 500);
  }, [load]);
  useEffect(() => () => window.clearTimeout(reloadTimer.current), []);
  useGatewaySubscription('agent', (ev) => {
    const p = (ev.payload || {}) as { stream?: string };
    if (p.stream === 'lifecycle') scheduleReload();
  });
  // A dropped frame makes any live view a guess — refetch the authoritative list.
  useGatewaySubscription('ava.gateway.gap', () => scheduleReload());

  const sid = route.sessionId;
  useEffect(() => {
    let live = true;
    setMessages([]);
    if (!client || !sid) return;
    void agentApi(client).sessions.history(sid)
      .then((got) => { if (live) setMessages(got?.messages || []); })
      .catch(() => { /* the thread shows its own empty state */ });
    return () => { live = false; };
  }, [client, sid]);

  // The selector runs HERE, once, and both columns read its answer: the list
  // gets the groups, the thread gets the enriched row — so a chat-origin
  // session is titled by its chat in both places rather than only where
  // somebody remembered.
  const groups = groupConsoleSessions(sessions, prefix, chats);
  const current = sid
    ? groups.flatMap((g) => g.sessions).find((x) => x.id === sid) || null
    : null;
  // From the KEY, not the enriched row: a deep link can name a session the
  // list has not loaded, and the reply hand-off must still work there.
  const cid = sid ? chatIdOfSession(sid, prefix) : null;

  return (
    <div className="agent-body agent-console">
      <SessionList
        groups={groups}
        activeId={sid}
        loading={loading}
        error={err}
        onOpen={(id) => onGo({ section: 'sessions', sessionId: id })}
      />
      <Thread
        session={current}
        sessionId={sid}
        messages={messages}
        chatId={cid}
        onOpenPanel={sid ? (p) => onGo({ section: 'sessions', sessionId: sid, panel: p }) : undefined}
        panel={route.panel}
      />
      {sid && route.panel && (
        <SidePanel
          sessionId={sid}
          panel={route.panel}
          onPanel={(p) => onGo({ section: 'sessions', sessionId: sid, panel: p })}
        />
      )}
    </div>
  );
}

function ActivitySection({ route, onGo }: SectionProps) {
  return (
    <div className="agent-body">
      {route.runId ? (
        <RunInspector
          runId={route.runId}
          onBack={() => onGo({ section: 'activity' })}
        />
      ) : (
        <ActivityList
          onOpenRun={(id) => onGo({ section: 'activity', runId: id })}
        />
      )}
    </div>
  );
}

function AutomationsSection({ route, onGo }: SectionProps) {
  return (
    <div className="agent-body">
      <AutomationsList
        activeId={route.jobId}
        onOpen={(id) => onGo({ section: 'automations', jobId: id })}
      />
    </div>
  );
}

export default AgentView;
