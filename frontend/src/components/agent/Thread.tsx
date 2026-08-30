import type { ChatMessage, Session } from '../../lib/agentApi';
import { EmptyState, Panel } from '../ui/layout';
import { Badge } from '../hub/ui/Badge';
import type { SidePanel as SidePanelId } from './agentRoute';
import { prChipView, SESSION_LABEL, type PrRef } from './agentView';

// One session's transcript — a window, never a mouth.
//
// There is deliberately NO composer here and no `readOnly` flag to say so:
// Chats is the one place you talk to the agent, and this console is where you
// watch it work. A chat-origin session gets `chatId`, which renders the
// hand-off — 'Reply in Chats' — instead of this tab growing an input of its
// own. (The old read-only side chat that reused this renderer is retired for
// the same reason: a read-only copy beside the thread was the thread twice.)

export function Thread({ session, sessionId, messages, loading, error, prs, chatId,
                        onOpenPanel, panel }: {
  /** The row this thread belongs to, when the list has already loaded it. */
  session?: Session | null;
  sessionId: string | null;
  messages: readonly ChatMessage[];
  loading?: boolean;
  error?: string;
  prs?: readonly PrRef[];
  /** Set for a chat-origin session: the chat to reply in. */
  chatId?: string | null;
  onOpenPanel?: (p: SidePanelId) => void;
  panel?: SidePanelId | null;
}) {
  if (!sessionId) {
    return (
      <div className="agent-thread">
        <EmptyState text="Pick a session on the left, or start a new one." />
      </div>
    );
  }
  return (
    <div className="agent-thread">
      <Panel
        // The session's NAME, falling back to its id only while the list is
        // still loading or when a deep link names a session the list does not
        // have. `s-3` is not something a person recognises, and titling a
        // thread with a database key is how a console starts feeling like a
        // debugger.
        title={session?.title || sessionId}
        subtitle={session ? SESSION_LABEL[session.state] : undefined}
        // Both the PR chips and the panel opener live in `right`. `Panel` has
        // one action slot and widening a primitive shared by every dashboard
        // for one caller's convenience is how a design system stops being one.
        right={(
          <span className="agent-thread-actions">
            {prs?.map((pr, i) => {
              const v = prChipView(pr);
              return (
                <span key={pr.number ?? i} title={v.title}>
                  <Badge tone={v.tone}>{v.label}</Badge>
                </span>
              );
            })}
            {chatId && (
              // The hand-off, not a composer. App.tsx owns both halves of the
              // jump (the chat store and hash segment 0), so this dispatches
              // rather than reaching into either.
              <button type="button" className="hub-btn ghost"
                      onClick={() => window.dispatchEvent(
                        new CustomEvent('ava:open-chat', { detail: { id: chatId } }))}>
                Reply in Chats
              </button>
            )}
            {onOpenPanel && !panel && (
              <button type="button" className="hub-btn ghost"
                      onClick={() => onOpenPanel('files')}>
                Open panel
              </button>
            )}
          </span>
        )}
      >
        {error ? (
          <p className="hub-msg err">{error}</p>
        ) : loading && !messages.length ? (
          <p className="agent-list-note">Loading the transcript…</p>
        ) : !messages.length ? (
          <EmptyState text="Nothing said yet." />
        ) : (
          <ol className="agent-msgs">
            {messages.map((m) => (
              <li key={m.id} className={`agent-msg role-${m.role}`}>
                <span className="agent-msg-role">{m.role}</span>
                <span className="agent-msg-text">{m.text}</span>
                {!!m.tools?.length && (
                  <span className="agent-msg-tools">
                    {m.tools.length} tool{m.tools.length === 1 ? '' : 's'}
                  </span>
                )}
              </li>
            ))}
          </ol>
        )}
      </Panel>
    </div>
  );
}
