import { useEffect, useRef, useState } from 'react';
import { getTheme } from '../lib/theme';

// Renders a third-party app's own web UI inside Ava's shell. The app is served
// SAME-ORIGIN via the bridge's /apps/<id>/ reverse-proxy, so it inherits Ava's
// session cookie (no cross-origin auth). Ava's current theme is passed as a
// ?theme= query param; the app may opt in to match Ava's look.
//
// The shell keeps visited frames mounted and toggles `active` instead of
// unmounting, so the app's in-page state (typed prompts, scroll, modals)
// survives switching to other Ava tabs. Inactive frames are display:none —
// still loaded, just not laid out.
export function AppFrame({ id, label, active = true }: { id: string; label: string; active?: boolean }) {
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading');
  const ref = useRef<HTMLIFrameElement>(null);

  useEffect(() => {
    setState('loading');
    // If the frame never fires `load` (app down / proxy 502), surface an error.
    const t = setTimeout(() => setState((s) => (s === 'loading' ? 'error' : s)), 12000);
    return () => clearTimeout(t);
  }, [id]);

  // v= busts HTML cached before the proxy sent Cache-Control: no-cache on app
  // pages — those poisoned entries pin the iframe to a stale bundle and are
  // never revalidated. Bump only if the embed contract changes again.

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
    const query = `theme=${getTheme()}&embedded=1&v=1`;
    fetch(`/api/apps/${encodeURIComponent(id)}/embed?${query}`, { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((j) => { if (live) setSrc(j.url); })
      // Fall back to the relative path so a bridge that predates this route still
      // renders, rather than showing an empty frame.
      .catch(() => { if (live) setSrc(`/apps/${encodeURIComponent(id)}/?${query}`); });
    return () => { live = false; };
  }, [id]);

  // Keep open apps in sync without reloading their frames and losing local state.
  // The initial URL covers first paint; the request/reply covers delayed hydration.
  const sendTheme = () => {
    if (!src) return;
    ref.current?.contentWindow?.postMessage(
      { type: 'ava:theme', theme: getTheme() },
      new URL(src, window.location.href).origin,
    );
  };
  useEffect(() => {
    if (!src) return;
    const origin = new URL(src, window.location.href).origin;
    const send = () => ref.current?.contentWindow?.postMessage(
      { type: 'ava:theme', theme: getTheme() }, origin,
    );
    const onRequest = (event: MessageEvent) => {
      if (event.source === ref.current?.contentWindow && event.origin === origin
        && event.data?.type === 'ava:theme-request') send();
    };
    const observer = new MutationObserver(send);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    window.addEventListener('message', onRequest);
    send();
    return () => {
      observer.disconnect();
      window.removeEventListener('message', onRequest);
    };
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
