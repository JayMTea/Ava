import { useEffect, useState } from 'react';
import { dash } from './dashboard/dashApi';
import { Icon } from '../lib/icons';

// Fallback surface for a connector that ships no UI (`ui.embed: none`). It shows
// the agent actions the app exposes — what Ava can do with it — derived from the
// connector registry. Invocation is via Ava (the agent), whose tool calls run
// the token-gated /internal/connector proxy; this panel is read-only.
export function ActionConsole({ id, label }: { id: string; label: string }) {
  const [actions, setActions] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    dash
      .connectors()
      .then((r) => {
        const row = r.connectors.find((c) => c.id === id);
        setActions(row?.actions ?? []);
      })
      .catch((e) => setError(String(e)));
  }, [id]);

  return (
    <div className="action-console">
      <div className="ac-head">
        <span className="ac-mark"><Icon name="panel" /></span>
        <div>
          <div className="ac-title">{label}</div>
          <div className="ac-sub">Ask Ava to use these — this app ships no UI of its own.</div>
        </div>
      </div>
      {error && <div className="panel-empty">Couldn’t load actions: {error}</div>}
      {actions && actions.length === 0 && (
        <div className="panel-empty">This app declares no agent actions.</div>
      )}
      {actions && actions.length > 0 && (
        <ul className="ac-list">
          {actions.map((a) => (
            <li key={a} className="ac-item">
              <Icon name="code" />
              <code>{a}</code>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
