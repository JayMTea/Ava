import { useEffect, useRef, useState } from 'react';
import { getTheme } from '../lib/theme';
import { connectAppTheme, sendAppTheme } from '../lib/embedTheme';

// Renders an app using the bridge-issued embed destination. A configured app
// origin isolates it from Ava; the bridge supplies scoped access and theme.
//
// The shell keeps visited frames mounted and toggles `active` instead of
// unmounting, so the app's in-page state (typed prompts, scroll, modals)
// survives switching to other Ava tabs. Inactive frames are display:none —
// still loaded, just not laid out.
export function appFrameDestination(base: string, id: string, path: string): string {
  const launch = new URL(base, window.location.href);
  const prefix = `/apps/${encodeURIComponent(id)}/`;
  const target = new URL(prefix + path.replace(/^\//, ''), launch.origin);
  if (!target.pathname.startsWith(prefix) || target.origin !== launch.origin) throw new Error('Invalid app destination');
  // Keep the bridge's credential and theme; an artifact cannot override either.
  for (const [key, value] of launch.searchParams) target.searchParams.set(key, value);
  return target.toString();
}

export function AppFrame({ id, label, active = true, path = '/' }: { id: string; label: string; active?: boolean; path?: string }) {
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading');
  const ref = useRef<HTMLIFrameElement>(null);

  useEffect(() => {
    setState('loading');
    // If the frame never fires `load` (app down / proxy 502), surface an error.
    const t = setTimeout(() => setState((s) => (s === 'loading' ? 'error' : s)), 12000);
    return () => clearTimeout(t);
  }, [id, path]);

  // The bridge decides where this frame loads from. With `apps.origin` configured
  // it returns an absolute URL on a second hostname carrying a short-lived,
  // cid-bound token — the app is then cross-origin with Ava and its JS gets no
  // session cookie and no `parent` access. Unconfigured, it returns the same
  // relative path as before, so there is no branch here for that case.
  //
  // Asked per mount rather than cached: the token is short-lived by design, and a
  // remount is exactly when a fresh one is wanted.
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setSrc(null);
    // v= busts HTML cached before the proxy sent Cache-Control: no-cache.
    const query = `theme=${getTheme()}&embedded=1&v=1`;
    fetch(`/api/apps/${encodeURIComponent(id)}/embed?${query}`, { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((j) => { if (live) setSrc(appFrameDestination(j.url, id, path)); })
      // Preserve the bridge's origin boundary when obtaining access fails.
      .catch(() => { if (live) setState('error'); });
    return () => { live = false; };
  }, [id, path]);

  // Keep open apps in sync without reloading their frames and losing local state.
  // The initial URL covers first paint; the request/reply covers delayed hydration.
  const sendTheme = () => {
    const frame = ref.current?.contentWindow;
    if (src && frame) sendAppTheme(frame, new URL(src, window.location.href).origin);
  };
  useEffect(() => {
    const frame = ref.current?.contentWindow;
    if (src && frame) return connectAppTheme(frame, new URL(src, window.location.href).origin);
  }, [src]);

  return (
    <div className="appframe" style={active ? undefined : { display: 'none' }}>
      {state === 'loading' && <div className="appframe-status">Loading {label}…</div>}
      {state === 'error' && (
        <div className="appframe-status appframe-error">
          {label} isn’t responding. Check that its service is running.
        </div>
      )}
      {src && (
        <iframe
          ref={ref}
          title={label}
          src={src}
          className="appframe-iframe"
          style={{ visibility: state === 'ready' ? 'visible' : 'hidden' }}
          // `allow-same-origin` means "keep your OWN origin", not "get Ava's".
          // With apps.origin configured the frame's own origin is the apps host, so
          // the app keeps its cookies and localStorage while being cross-origin
          // with Ava — no session cookie, no parent access. UNCONFIGURED, the proxy
          // serves it from Ava's origin and this pairing does hand it Ava's session;
          // that is the hole apps.origin exists to close, and Setup → Connectors
          // says so — see the `apps_origin` banner in ConnectorsPanel.tsx, which
          // renders it whenever an app is embedded and the origin is unset. That
          // claim was aspirational for a long time: the bridge shipped
          // `apps_origin.warning()` on /api/apps and nothing anywhere read it.
          sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-downloads"
          onLoad={() => { setState('ready'); sendTheme(); }}
        />
      )}
    </div>
  );
}
