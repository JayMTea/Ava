import { useState } from 'react';

import { SIDE_PANELS, type SidePanel as PanelId } from './agentRoute';
import { FilesPanel } from './FilesPanel';
import { ReviewPanel } from './ReviewPanel';
import { TasksPanel } from './TasksPanel';
import { TerminalPanel } from './TerminalPanel';

// The working surface attached to a session.
//
// Addressable AS A SEGMENT ON THE SESSION (`#agent/s/<id>/review`) rather than
// as component state, because "this session, Review open" is a thing you send
// to somebody. It is also what makes the mobile layout free: the same address
// becomes a full-screen sheet below the breakpoint, with Back to dismiss.
//
// EVERY PANEL HERE IS NATIVE. It used to say "even for the two panels whose
// CONTENT is an embed" — there are none left. The Browser panel embedded
// /apps/openclaw/browser, a path no install can serve because OpenClaw is
// deliberately not a connector, so it 404'd everywhere and was removed along
// with the GatewayFrame/embedBridge pair it was the only user of.

const LABEL: Record<PanelId, string> = {
  terminal: 'Terminal',
  files: 'Files',
  tasks: 'Tasks',
  review: 'Review',
};

export function SidePanel({ sessionId, panel, onPanel }: {
  sessionId: string;
  panel: PanelId;
  onPanel: (p: PanelId | null) => void;
}) {
  // Panel-local, deliberately NOT in the address. Which file is open is a
  // working position, not a destination — and a fifth URL segment would have to
  // be parsed, canonicalised and kept valid against a list that loads
  // asynchronously, for something nobody bookmarks.
  //
  // The selection carries its OWN session id and is read back through a
  // comparison, rather than being cleared by an effect when `sessionId`
  // changes. Two reasons: there is no render where a stale path is briefly
  // live (an effect clears it one tick late), and there is no effect whose only
  // dependency is a trigger it never reads — which is both hard to lint and
  // hard for the next reader to trust.
  const [picked, setPicked] = useState<{ sid: string; path: string } | null>(null);
  const filePath = picked && picked.sid === sessionId ? picked.path : null;

  return (
    <section className="agent-side" aria-label="Session panel">
      <div className="agent-side-bar">
        <nav className="hub-subtabs" aria-label="Panels">
          {SIDE_PANELS.map((p) => (
            <button
              type="button"
              key={p}
              className={`hub-subtab${p === panel ? ' on' : ''}`}
              aria-current={p === panel ? 'page' : undefined}
              onClick={() => onPanel(p)}
            >
              {LABEL[p]}
            </button>
          ))}
        </nav>
        <button
          type="button"
          className="hub-btn ghost agent-side-close"
          onClick={() => onPanel(null)}
          aria-label="Close panel"
        >
          Close
        </button>
      </div>

      <div className="agent-side-body">
        {panel === 'files' && (
          <FilesPanel
            sessionId={sessionId}
            onOpen={(path) => { setPicked({ sid: sessionId, path }); onPanel('review'); }}
          />
        )}
        {panel === 'review' && (
          <ReviewPanel
            sessionId={sessionId}
            path={filePath ?? undefined}
            onPick={(path) => setPicked({ sid: sessionId, path })}
          />
        )}
        {panel === 'tasks' && <TasksPanel sessionId={sessionId} />}
        {panel === 'terminal' && <TerminalPanel sessionId={sessionId} />}
      </div>
    </section>
  );
}
