import { EmptyState } from '../dashboard/layout';
import type { ConsoleGroup } from './agentView';
import { SessionRow } from './SessionRow';

// The left column: what the agent has open, grouped.
//
// Deliberately dumb — every decision (which bucket, what order, what is hidden,
// what a chat-origin row is called) lives in `agentView.groupConsoleSessions`
// where vitest can reach it; the caller runs the selector and hands the groups
// in. What is left here is markup, which is the part headless Chromium is good
// at checking.
//
// There is deliberately no 'New session' button. Sessions are made by giving
// the agent work — a chat, an automation, a channel — never by an empty shell
// opened from here; the one that used to sit at the top was permanently
// disabled because nothing ever passed it a handler.

export function SessionList({ groups, activeId, loading, error, onOpen }: {
  groups: readonly ConsoleGroup[];
  activeId: string | null;
  loading?: boolean;
  error?: string;
  onOpen: (id: string) => void;
}) {
  return (
    <aside className="agent-list" aria-label="Sessions">
      {error ? (
        // Surfaced, never swallowed — the same rule every `useResource` call
        // site in Setup follows.
        <p className="hub-msg err">{error}</p>
      ) : loading && !groups.length ? (
        <p className="agent-list-note">Loading sessions…</p>
      ) : !groups.length ? (
        <>
          <EmptyState text="Sessions appear when Chats, automations, or channels give the agent work." />
          <p className="agent-list-note"><a href="#chat">Go to Chats</a></p>
        </>
      ) : (
        groups.map((g) => (
          <section key={g.id} className="hub-group">
            <h3 className="hub-group-title">{g.label}</h3>
            <div className="agent-rows">
              {g.sessions.map((s) => (
                <SessionRow
                  key={s.id}
                  session={s}
                  active={s.id === activeId}
                  onOpen={onOpen}
                />
              ))}
            </div>
          </section>
        ))
      )}
    </aside>
  );
}
