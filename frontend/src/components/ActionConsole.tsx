import { useEffect, useState } from 'react';
import { Icon } from '../lib/icons';
import { appAccent } from '../lib/appColor';
import { useBrandName } from '../lib/brandContext';

// Fallback surface for a connector that ships no UI (`ui.embed: none`). It shows
// the agent actions the app exposes — what Ava can do with it — derived from the
// connector registry. Invocation is via Ava (the agent), whose tool calls run
// the token-gated /internal/connector proxy; this panel is read-only.
export function ActionConsole({ id, label }: { id: string; label: string }) {
  const brand = useBrandName();
  const [actions, setActions] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`/api/apps/${encodeURIComponent(id)}/actions`, {
      credentials: 'same-origin',
      cache: 'no-store',
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
      .then((r: { actions?: string[] }) => setActions(r.actions ?? []))
      .catch((e) => setError(String(e)));
  }, [id]);

  return (
    <div className="action-console">
      <div className="ac-head">
        {/* App identity accent — this whole surface belongs to the app. */}
        <span className="ac-mark" style={{ color: appAccent(id) }}><Icon name="panel" /></span>
        <div>
          <div className="ac-title">{label}</div>
          <div className="ac-sub">Ask {brand} to use these — this app ships no UI of its own.</div>
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
