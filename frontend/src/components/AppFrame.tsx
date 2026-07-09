import { useEffect, useRef, useState } from 'react';

// Renders a third-party app's own web UI inside Ava's shell. The app is served
// SAME-ORIGIN via the bridge's /apps/<id>/ reverse-proxy, so it inherits Ava's
// session cookie (no cross-origin auth). Ava's current theme is passed as a
// ?theme= query param; the app may opt in to match Ava's look.
export function AppFrame({ id, label }: { id: string; label: string }) {
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading');
  const ref = useRef<HTMLIFrameElement>(null);

  useEffect(() => {
    setState('loading');
    // If the frame never fires `load` (app down / proxy 502), surface an error.
    const t = setTimeout(() => setState((s) => (s === 'loading' ? 'error' : s)), 12000);
    return () => clearTimeout(t);
  }, [id]);

  const theme = document.documentElement.dataset.theme
    || (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  // v= busts HTML cached before the proxy sent Cache-Control: no-cache on app
  // pages — those poisoned entries pin the iframe to a stale bundle and are
  // never revalidated. Bump only if the embed contract changes again.
  const src = `/apps/${encodeURIComponent(id)}/?theme=${theme}&embedded=1&v=1`;

  return (
    <div className="appframe">
      {state === 'loading' && <div className="appframe-status">Loading {label}…</div>}
      {state === 'error' && (
        <div className="appframe-status appframe-error">
          {label} isn’t responding. Check that its service is running.
        </div>
      )}
      <iframe
        ref={ref}
        title={label}
        src={src}
        className="appframe-iframe"
        style={{ visibility: state === 'ready' ? 'visible' : 'hidden' }}
        // Same-origin (via proxy) so allow-same-origin is required for the app's
        // own cookies/storage; scripts + forms for a normal web app.
        sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-downloads"
        onLoad={() => setState('ready')}
      />
    </div>
  );
}
