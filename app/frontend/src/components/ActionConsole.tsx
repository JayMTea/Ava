import { useEffect, useState } from 'react';
import { Icon } from '../lib/icons';
import { appAccent } from '../lib/appColor';
import { useBrandName } from '../lib/brandContext';
import {
  type Surface,
  TIER_HINT,
  TRANSPORT_HINT,
  TRANSPORT_LABEL,
  consoleState,
  parseSurface,
} from '../lib/agentSurface';

// The whole tile for a connector that ships no UI (`ui.embed: none`) — not a
// fallback, the product. It shows what Ava can actually do with the app.
//
// For a connector whose tools are resolved at run time (a real `mcp:` server,
// or the ava-tools/1 facade) the backend ASKS the app, so this is its live
// surface rather than the manifest's single synthetic bridge row. Each row
// carries the tier Ava will ENFORCE and whether it stops to ask first, so the
// panel answers "will this just happen?" and not merely "does this exist".
//
// Invocation is via Ava (the agent), whose tool calls run the token-gated
// /internal/connector proxy; this panel is read-only.
export function ActionConsole({ id, label }: { id: string; label: string }) {
  const brand = useBrandName();
  const [surface, setSurface] = useState<Surface | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);

  useEffect(() => {
    // Discovery is a network hop now, and the shell reuses this component
    // across app switches. Without the guard a slow answer for the app the
    // owner just left lands in the panel of the one they opened.
    let current = true;
    setSurface(null);
    setFetchError(null);
    fetch(`/api/apps/${encodeURIComponent(id)}/actions`, {
      credentials: 'same-origin',
      cache: 'no-store',
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
      .then((r) => current && setSurface(parseSurface(r)))
      .catch((e) => current && setFetchError(String(e)));
    return () => {
      current = false;
    };
  }, [id]);

  const state = consoleState(surface, fetchError);
  const transport = surface?.transport ?? 'none';

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

      {state.kind === 'loading' && (
        <div className="panel-empty">Asking {label} what it can do…</div>
      )}

      {state.kind === 'unavailable' && (
        <div className="panel-empty">Couldn’t load actions: {state.detail}</div>
      )}

      {state.kind === 'tools' && (
        <>
          <div className="ac-meta">
            {state.tools.length} {state.tools.length === 1 ? 'tool' : 'tools'}
            {TRANSPORT_LABEL[transport] ? (
              <> · <span title={TRANSPORT_HINT[transport]}>{TRANSPORT_LABEL[transport]}</span></>
            ) : null}
            {state.stale ? ' · last known list' : null}
          </div>
          {state.stale && state.detail && (
            <div className="ac-warn">
              <Icon name="alert" />
              <span>{label} didn’t answer, so this is the list it served last — {state.detail}</span>
            </div>
          )}
          <ul className="ac-list">
            {state.tools.map((t) => (
              <li key={t.name} className="ac-item">
                <Icon name="code" />
                <div className="ac-body">
                  <div className="ac-name">
                    <code>{t.name}</code>
                    {t.access && (
                      <span
                        className={`ac-tier ac-tier-${t.access}`}
                        title={TIER_HINT[t.access] || t.access}
                      >
                        {t.access}
                      </span>
                    )}
                    {t.confirm && (
                      <span className="ac-asks" title={`${brand} asks you before running this`}>
                        <Icon name="lock" /> asks first
                      </span>
                    )}
                  </div>
                  {t.description && <div className="ac-desc">{t.description}</div>}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      {state.kind === 'unreachable' && (
        <div className="panel-empty">
          Couldn’t reach {label} to ask what it can do — {state.detail}
        </div>
      )}

      {state.kind === 'silent' && (
        <div className="panel-empty">{label} answered, and lists no tools.</div>
      )}

      {state.kind === 'none' && (
        <div className="panel-empty">This app declares no agent actions.</div>
      )}
    </div>
  );
}
