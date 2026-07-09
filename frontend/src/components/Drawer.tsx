import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { AppEntry, ChatSummary } from '../lib/types';
import { Icon } from '../lib/icons';

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
      <button
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
            <button
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
  // Built-in tabs that always ship in the shell. Everything else in the rail is
  // derived from the connector app registry (/api/apps), so a new app appears by
  // dropping a connector folder — no edits here. The assistant tab is brand-named.
  const BUILTIN: { id: string; label: string; icon: string }[] = [
    { id: 'vitals', label: 'Vitals', icon: 'gauge' },
    { id: 'ops', label: 'Operations', icon: 'activity' },
    { id: 'chat', label: `${brand} — Assistant`, icon: 'bot' },
  ];
  // Title for the panel header: built-in label or the active app's label.
  const titles: Record<string, string> = Object.fromEntries([
    ...BUILTIN.map((b) => [b.id, b.label]),
    ['hub', 'Setup'],
    ...apps.map((a) => [a.id, a.label]),
  ]);

  const railBtn = (id: string, label: string, icon: string) => (
    <button
      key={id}
      className={'rail-btn' + (view === id ? ' active' : '')}
      title={label}
      aria-label={label}
      onClick={() => onView(id)}
    >
      <Icon name={icon} />
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
          title={open ? 'Close sidebar' : 'Open sidebar'}
          aria-label={open ? 'Close sidebar' : 'Open sidebar'}
          aria-expanded={open}
          onClick={onToggle}
        >
          <Icon name="sidebar" />
        </button>
        <button
          className="rail-btn rail-new"
          title="New chat"
          aria-label="New chat"
          onClick={onNewChat}
        >
          <Icon name="plus" />
        </button>
        <div className="rail-tabs">
          {railBtn('chat', `${brand} — Assistant`, 'bot')}
          {/* App tabs — derived from the connector registry, below the chat icon. */}
          {apps
            .filter((a) => a.section !== 'core')
            .map((a) => railBtn(a.id, a.label, a.icon))}
        </div>
        <div className="rail-spacer" />
        <div className="rail-foot">
          <RailFlyout
            items={[
              { id: 'vitals', label: 'Vitals', icon: 'gauge' },
              { id: 'ops', label: 'Operations', icon: 'activity' },
              { id: 'hub', label: 'Setup', icon: 'sliders' },
            ]}
            view={view}
            onView={onView}
          />
        </div>
      </div>

      {/* Content panel — brand + (for the Assistant tab) the chat history. */}
      <div className="side-panel">
        <div className="panel-head">
          <div className="brand">
            <span className="brand-mark">
              <Icon name="bot" />
            </span>
            <span className="brand-name">{titles[view] || brand}</span>
          </div>
        </div>

        {view === 'chat' ? (
          <>
            <button className="newchat-btn" title="New chat" aria-label="New chat" onClick={onNewChat}>
              <Icon name="plus" />
              <span>New chat</span>
            </button>
            <div className="draw-sub">Recents</div>
            <div id="chatList">
              {chats.length === 0 ? (
                <div className="draw-empty">No conversations yet</div>
              ) : (
                chats.map((it) => (
                  <div
                    key={it.id}
                    className={'chatitem' + (it.id === currentChatId ? ' active' : '')}
                    onClick={() => onOpenChat(it.id)}
                  >
                    <div className="ci-main">
                      <div className="ci-title">{it.title || 'New chat'}</div>
                    </div>
                    <button
                      className="del"
                      title="Delete chat"
                      aria-label="Delete chat"
                      onClick={(e) => {
                        e.stopPropagation();
                        onDeleteChat(it.id);
                      }}
                    >
                      <Icon name="trash" />
                    </button>
                  </div>
                ))
              )}
            </div>
          </>
        ) : (
          <div className="panel-empty">Open the classic view at /legacy.</div>
        )}
      </div>
    </aside>
  );
}
