import { useEffect, useRef, useState } from 'react';
import type { MouseEvent as ReactMouseEvent } from 'react';
import { createPortal } from 'react-dom';
import type { AppEntry, ChatSummary } from '../lib/types';
import { Icon } from '../lib/icons';
import { AppDot, appAccent, appIcon } from '../lib/appColor';

// Rail foot flyout: the sliders icon reveals the "system" destinations (Vitals,
// Operations, Setup) as a hover/click pop-up, so the rail itself stays just chat
// + the user's connected apps. Rendered through a portal (the drawer clips
// overflow) and positioned against the trigger, opening up-and-to-the-right.
type MenuItem = { id: string; label: string; icon: string };
function RailFlyout({
  items, view, onView,
}: {
  items: MenuItem[]; view: string; onView: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState({ left: 0, bottom: 0 });
  const wrapRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<number | undefined>(undefined);
  const active = items.some((it) => it.id === view);

  const place = () => {
    const r = wrapRef.current?.getBoundingClientRect();
    if (r) setPos({ left: r.right + 8, bottom: window.innerHeight - r.bottom });
  };
  const openMenu = () => { window.clearTimeout(closeTimer.current); place(); setOpen(true); };
  const scheduleClose = () => { closeTimer.current = window.setTimeout(() => setOpen(false), 140); };

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    const onDown = (e: PointerEvent) => {
      const t = e.target as Element;
      if (!wrapRef.current?.contains(t) && !t.closest?.('.rail-menu')) setOpen(false);
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('pointerdown', onDown);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('pointerdown', onDown);
    };
  }, [open]);

  return (
    <div className="rail-menu-wrap" ref={wrapRef} onMouseEnter={openMenu} onMouseLeave={scheduleClose}>
      <button type="button"
        className={'rail-btn' + (active ? ' active' : '')}
        aria-haspopup="menu"
        aria-expanded={open}
        title="Settings & dashboards"
        aria-label="Settings & dashboards"
        onClick={() => (open ? setOpen(false) : openMenu())}
      >
        <Icon name="sliders" />
      </button>
      {open && createPortal(
        <div
          className="rail-menu"
          role="menu"
          style={{ left: pos.left, bottom: pos.bottom }}
          onMouseEnter={openMenu}
          onMouseLeave={scheduleClose}
        >
          {items.map((it) => (
            <button type="button"
              key={it.id}
              role="menuitem"
              className={'rail-menu-item' + (view === it.id ? ' active' : '')}
              onClick={() => { onView(it.id); setOpen(false); }}
            >
              <Icon name={it.icon} />
              <span>{it.label}</span>
            </button>
          ))}
        </div>,
        document.body,
      )}
    </div>
  );
}

interface Props {
  open: boolean;
  onToggle: () => void;
  apps: AppEntry[];
  brand?: string;
  view: string;
  onView: (v: string) => void;
  chats: ChatSummary[];
  currentChatId: string | null;
  onNewChat: () => void;
  onOpenChat: (id: string) => void;
  onDeleteChat: (id: string) => void;
}

export function Drawer({
  open,
  onToggle,
  apps,
  brand = 'Ava',
  view,
  onView,
  chats,
  currentChatId,
  onNewChat,
  onOpenChat,
  onDeleteChat,
}: Props) {
  // Hover tooltip for the rail icons (claude.ai style). One shared label,
  // portalled to <body> — the drawer clips overflow — and fixed at the rail's
  // right edge, vertically centered on the hovered button.
  const [tip, setTip] = useState<{ label: string; top: number } | null>(null);
  // Expanded-panel search: the magnifier in the head row reveals an input that
  // filters the Recents list by title.
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const tipProps = (label: string) => ({
    onMouseEnter: (e: ReactMouseEvent<HTMLElement>) => {
      const r = e.currentTarget.getBoundingClientRect();
      setTip({ label, top: r.top + r.height / 2 });
    },
    onMouseLeave: () => setTip(null),
  });

  // Primary destinations in the expanded panel (claude.ai's Chats / Projects /
  // Artifacts / Customize slot). Everything else is derived from the connector
  // app registry (/api/apps), so a new app appears by dropping a connector
  // folder — no edits here.
  const NAV: { id: string; label: string; icon: string }[] = [
    { id: 'chat', label: 'Chats', icon: 'chats' },
    { id: 'vitals', label: 'Vitals', icon: 'gauge' },
    { id: 'ops', label: 'Operations', icon: 'activity' },
    { id: 'data', label: 'Data', icon: 'db' },
    { id: 'hub', label: 'Setup', icon: 'sliders' },
  ];
  const userApps = apps.filter((a) => a.section !== 'core');
  const q = query.trim().toLowerCase();
  const visibleChats = q
    ? chats.filter((c) => (c.title || 'New chat').toLowerCase().includes(q))
    : chats;

  // `accent` marks connected-app tabs with the app's identity color (a small
  // corner dot), so app destinations read as the app's, not Ava's.
  const railBtn = (id: string, label: string, icon: string, accent?: string) => (
    <button
      key={id}
      className={'rail-btn' + (view === id ? ' active' : '')}
      aria-label={label}
      {...tipProps(label)}
      onClick={() => onView(id)}
    >
      <Icon name={icon} />
      {accent && <AppDot accent={accent} className="rail-dot" />}
    </button>
  );

  return (
    <aside id="drawer" className={open ? 'open' : ''}>
      {/* Narrow icon rail (claude.ai style) — panel toggle on top, then new chat,
          the Assistant, and the user's connected apps. Vitals / Operations / Setup
          live in the settings flyout at the foot. */}
      <div className="side-rail">
        <button
          className="rail-btn rail-toggle"
          aria-label={open ? 'Close sidebar' : 'Open sidebar'}
          aria-expanded={open}
          {...tipProps(open ? 'Close sidebar' : 'Open sidebar')}
          onClick={() => { onToggle(); setTip(null); }}
        >
          <Icon name="sidebar" />
        </button>
        <button
          className="rail-btn rail-new"
          aria-label="New chat"
          {...tipProps('New chat')}
          onClick={onNewChat}
        >
          <Icon name="plus" />
        </button>
        <div className="rail-tabs">
          {railBtn('chat', `${brand} — Assistant`, 'bot')}
          {/* App tabs — derived from the connector registry, below the chat icon. */}
          {userApps.map((a) => railBtn(a.id, a.label, appIcon(a), appAccent(a)))}
        </div>
        {tip && createPortal(
          <div className="rail-tip" style={{ top: tip.top }}>{tip.label}</div>,
          document.body,
        )}
        <div className="rail-spacer" />
        <div className="rail-foot">
          <RailFlyout
            items={[
              { id: 'vitals', label: 'Vitals', icon: 'gauge' },
              { id: 'ops', label: 'Operations', icon: 'activity' },
              { id: 'data', label: 'Data', icon: 'db' },
              { id: 'hub', label: 'Setup', icon: 'sliders' },
            ]}
            view={view}
            onView={onView}
          />
        </div>
      </div>

      {/* Expanded panel (claude.ai layout): wordmark head with search + collapse,
          the circled "+ New chat", primary destinations, then the sectioned
          groups — connected Apps and the Recents chat history. */}
      <div className="side-panel">
        <div className="panel-head">
          <span className="brand-word">{brand}</span>
          <div className="panel-head-actions">
            <button type="button"
              className={'ibtn' + (searchOpen ? ' on' : '')}
              title="Search chats"
              aria-label="Search chats"
              aria-expanded={searchOpen}
              onClick={() => { setSearchOpen((o) => !o); if (searchOpen) setQuery(''); }}
            >
              <Icon name="search" />
            </button>
            <button type="button"
              className="ibtn"
              title="Close sidebar"
              aria-label="Close sidebar"
              onClick={onToggle}
            >
              <Icon name="sidebar" />
            </button>
          </div>
        </div>

        {searchOpen && (
          <input
            className="panel-search"
            type="search"
            placeholder="Search chats…"
            value={query}
            autoFocus
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Escape') { setSearchOpen(false); setQuery(''); } }}
          />
        )}

        <button type="button" className="nav-item nav-new" aria-label="New chat" onClick={onNewChat}>
          <span className="nav-new-ic"><Icon name="plus" /></span>
          <span>New chat</span>
        </button>

        <nav className="nav-list" aria-label="Primary">
          {NAV.map((it) => (
            <button type="button"
              key={it.id}
              className={'nav-item' + (view === it.id ? ' active' : '')}
              onClick={() => onView(it.id)}
            >
              <Icon name={it.icon} className="nav-ic" />
              <span>{it.label}</span>
            </button>
          ))}
        </nav>

        {userApps.length > 0 && (
          <>
            <div className="draw-sub">Apps</div>
            <nav className="nav-list" aria-label="Apps">
              {userApps.map((a) => (
                <button type="button"
                  key={a.id}
                  className={'nav-item' + (view === a.id ? ' active' : '')}
                  onClick={() => onView(a.id)}
                >
                  <Icon name={appIcon(a)} className="nav-ic" />
                  <span>{a.label}</span>
                  <AppDot accent={appAccent(a)} className="nav-app-dot" />
                </button>
              ))}
            </nav>
          </>
        )}

        <div className="draw-sub">Recents</div>
        <div id="chatList">
          {visibleChats.length === 0 ? (
            <div className="draw-empty">{q ? 'No matching chats' : 'No conversations yet'}</div>
          ) : (
            visibleChats.map((it) => (
              // A real <button>, not a <div onClick>. The row was a div because a
              // button cannot nest a button — so the fix is to UNNEST rather than
              // bolt tabIndex + onKeyDown onto a div. Before this, `tabIndex`
              // appeared zero times in the whole frontend: the only
              // keyboard-reachable control per row was the delete button, which
              // is opacity:0 until hover. So a keyboard user could reach exactly
              // one invisible control per conversation, and it deleted it.
              // WCAG 2.1.1 Level A, with no alternate route — DataView's chat
              // rows offer export and delete, but never open.
              <div
                key={it.id}
                className={'chatitem-wrap' + (it.id === currentChatId ? ' active' : '')}
              >
                <button
                  type="button"
                  className="chatitem"
                  aria-current={it.id === currentChatId ? 'page' : undefined}
                  onClick={() => onOpenChat(it.id)}
                >
                  <div className="ci-main">
                    <div className="ci-title">{it.title || 'New chat'}</div>
                  </div>
                </button>
                <button
                  type="button"
                  className="del"
                  title="Delete chat"
                  aria-label={`Delete chat: ${it.title || 'New chat'}`}
                  onClick={() => {
                    // Reachable by keyboard now, so a stray Enter must not be
                    // silent data loss — every other destructive action in the
                    // app confirms (DataView, ConnectorsPanel, AgentPanel).
                    if (window.confirm(`Delete "${it.title || 'New chat'}"? This cannot be undone.`)) {
                      onDeleteChat(it.id);
                    }
                  }}
                >
                  <Icon name="trash" />
                </button>
              </div>
            ))
          )}
        </div>
      </div>
    </aside>
  );
}
