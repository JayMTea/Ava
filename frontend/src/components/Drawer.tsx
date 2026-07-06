import type { AppEntry, ChatSummary } from '../lib/types';
import { Icon } from '../lib/icons';

interface Props {
  open: boolean;
  onClose: () => void;
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
  onClose,
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
      {/* Narrow icon rail — one icon per page (Claude style). */}
      <div className="side-rail">
        <div className="rail-tabs">
          {/* Vitals + Operations, then a divider before the app tabs. */}
          {railBtn('vitals', 'Vitals', 'gauge')}
          {railBtn('ops', 'Operations', 'activity')}
          <div className="rail-div" />
          {railBtn('chat', `${brand} — Assistant`, 'bot')}
          {/* App tabs — derived from the connector registry. */}
          {apps
            .filter((a) => a.section !== 'core')
            .map((a) => railBtn(a.id, a.label, a.icon))}
        </div>
        <div className="rail-spacer" />
        <form className="rail-foot" method="post" action="/logout">
          <button type="submit" className="rail-btn" title="Sign out" aria-label="Sign out">
            <Icon name="lock" />
          </button>
        </form>
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
          <button
            id="closeDrawer"
            className="ibtn"
            style={{ width: 32, height: 32 }}
            aria-label="Close"
            onClick={onClose}
          >
            <Icon name="close" />
          </button>
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
