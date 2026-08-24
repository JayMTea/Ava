import { Badge } from '../hub/ui/Badge';
import { Tile } from '../hub/ui/Tile';
import {
  type ConsoleSession, SESSION_LABEL, SESSION_TONE, sessionIcon, unreadLabel,
} from './agentView';

// One session in the list.
//
// Built from `Tile` + `Badge` + a tone dot — the same three atoms every Setup
// row uses. `tests/test_hub_uniformity.py` forbids re-handrolling those, and the
// reason is visible here: a session row and a connector row should read as the
// same product, not as two people's ideas of a list item.

export function SessionRow({ session, active, onOpen }: {
  session: ConsoleSession;
  active: boolean;
  onOpen: (id: string) => void;
}) {
  const tone = SESSION_TONE[session.state];
  const unread = Math.max(0, session.unread || 0);
  return (
    <button
      type="button"
      className={`agent-row${active ? ' on' : ''}`}
      aria-current={active ? 'true' : undefined}
      onClick={() => onOpen(session.id)}
    >
      {/* `icon` is set by groupConsoleSessions on chat-origin rows — the
          Chats glyph says where this session came from and where replies go. */}
      <Tile icon={session.icon || sessionIcon(session.kind)} tone={tone} size={26} />
      <span className="agent-row-body">
        <span className="agent-row-title">{session.title || session.id}</span>
        <span className="agent-row-meta">
          {/* The state is a WORD, not only a dot. A colour alone is unreadable
              to anyone who cannot see it, and ambiguous to everyone else. */}
          <span className={`agent-row-dot tone-${tone}`} aria-hidden="true" />
          {SESSION_LABEL[session.state]}
          {session.note && (
            // 'deleted from Chats' — the chat is gone but the agent-side
            // transcript is a server fact this console still audits.
            <>
              <span className="meta-sep">·</span>
              <span>{session.note}</span>
            </>
          )}
          {session.draft && (
            <>
              <span className="meta-sep">·</span>
              <span title="unsent draft">draft</span>
            </>
          )}
        </span>
      </span>
      {unread > 0 && (
        // The count carries its own label so the badge is not a bare number
        // whose meaning lives only in the layout.
        <span aria-label={unreadLabel(unread)}>
          <Badge tone="accent">{unread}</Badge>
        </span>
      )}
    </button>
  );
}
