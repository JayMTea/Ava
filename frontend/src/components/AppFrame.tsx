import { useCallback, useEffect, useRef, useState } from 'react';
import {
  type EmbedGrant,
  type EmbedLease,
  expiredFramePath,
  keepaliveIntervalMs,
  keepaliveUrl,
  leaseExpired,
  leaseFrom,
  REMINT_COOLDOWN_MS,
} from '../lib/embedLease';
import { connectAppTheme, sendAppTheme } from '../lib/embedTheme';
import { getTheme } from '../lib/theme';
import { frameNavigation } from '../lib/embedNavigation';

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

// How long a frame may take to fire `load` before the wait is named, and before
// the app is called dead. The first version had one timer at twelve seconds and
// said "check that its service is running" — which told a phone on a tailnet
// relay that a healthy app was down, and hid the frame while saying it.
const SLOW_AFTER_MS = 12_000;
const DEAD_AFTER_MS = 45_000;

type FrameState = 'loading' | 'slow' | 'ready' | 'error';

export function AppFrame({ id, label, active = true, path = '/', onNavigate }: { id: string; label: string; active?: boolean; path?: string; onNavigate?: (id: string, path: string) => void }) {
  const [state, setState] = useState<FrameState>('loading');
  const [lease, setLease] = useState<EmbedLease | null>(null);
  const [attempt, setAttempt] = useState(0);
  const ref = useRef<HTMLIFrameElement>(null);
  // Has the CURRENT lease's document fired `load`? Decides which lifetime applies.
  const loaded = useRef(false);
  // Where the next mint opens the app: the prop, or where a dead frame said it was.
  const wanted = useRef(path);
  const lastMint = useRef(0);
  // A late answer from a superseded mint must not overwrite the current lease.
  const generation = useRef(0);
  const stateRef = useRef<FrameState>(state);
  stateRef.current = state;

  // The bridge decides where this frame loads from. With `apps.origin` configured
  // it returns an absolute URL on a second hostname carrying a short-lived,
  // cid-bound token — the app is then cross-origin with Ava and its JS gets no
  // session cookie and no `parent` access. Unconfigured, it returns the same
  // relative path as before, so there is no branch here for that case.
  //
  // Asked per mount rather than cached — and again whenever the lease it handed
  // out is past saving (see below): the token is short-lived by design, and a
  // remount, a resume from the background or a frame that says it died are
  // exactly when a fresh one is wanted.
  const mint = useCallback(() => {
    const gen = ++generation.current;
    lastMint.current = Date.now();
    loaded.current = false;
    setState('loading');
    setAttempt((n) => n + 1);
    // v= busts HTML cached before the proxy sent Cache-Control: no-cache.
    const query = `theme=${getTheme()}&embedded=1&v=1`;
    fetch(`/api/apps/${encodeURIComponent(id)}/embed?${query}`, { credentials: 'same-origin' })
      .then((r) => (r.ok ? (r.json() as Promise<EmbedGrant>) : Promise.reject(new Error(String(r.status)))))
      .then((grant) => {
        if (gen !== generation.current) return;
        const src = appFrameDestination(grant.url, id, wanted.current);
        setLease(leaseFrom(grant, src, new URL(src, window.location.href).origin, Date.now()));
      })
      // Preserve the bridge's origin boundary when obtaining access fails.
      .catch(() => { if (gen === generation.current) setState('error'); });
  }, [id]);

  useEffect(() => {
    // A route reported by this frame is already on screen. Only an external
    // destination (a bookmark or browser history) needs a new navigation.
    if (generation.current > 0 && wanted.current === path) return;
    wanted.current = path;
    mint();
  }, [path, mint]);

  useEffect(() => {
    const frame = ref.current?.contentWindow;
    if (!lease || !frame) return;
    const onMessage = (event: MessageEvent) => {
      const next = frameNavigation(event, frame, lease.origin, id);
      if (next === null) return;
      wanted.current = next;
      onNavigate?.(id, next);
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [lease, id, onNavigate]);

  // The wait, named honestly: slow first, dead later. Reset on every attempt.
  useEffect(() => {
    if (attempt === 0) return;
    const slow = setTimeout(() => setState((s) => (s === 'loading' ? 'slow' : s)), SLOW_AFTER_MS);
    const dead = setTimeout(() => setState((s) => (s === 'loading' || s === 'slow' ? 'error' : s)), DEAD_AFTER_MS);
    return () => { clearTimeout(slow); clearTimeout(dead); };
  }, [attempt]);

  // Coming back from the background. A phone freezes a hidden page: no request
  // goes out, so nothing renews the cookie, and the first tap after unlocking
  // used to land on a refusal with no reload button to get out of it. Mint again
  // if the lease is past saving or the frame had already given up; leave a live
  // one alone, because a reload is exactly the in-page state a kept-alive frame
  // exists to preserve.
  useEffect(() => {
    const resume = () => {
      if (document.visibilityState !== 'visible') return;
      if (stateRef.current === 'error' || (lease && leaseExpired(lease, loaded.current, Date.now()))) mint();
    };
    document.addEventListener('visibilitychange', resume);
    window.addEventListener('pageshow', resume);
    return () => {
      document.removeEventListener('visibilitychange', resume);
      window.removeEventListener('pageshow', resume);
    };
  }, [lease, mint]);

  // Opening a tile that had given up tries it again; a tile that is fine is left as it is.
  useEffect(() => {
    if (active && stateRef.current === 'error') mint();
  }, [active, mint]);

  // A dead frame says so. The bridge answers a framed navigation on a token it no
  // longer accepts with a page that posts {type:'ava:embed-expired', cid, path} to
  // this origin (ava_bridge/apps_origin.reconnect_page). Only our own frame's
  // window, only from the lease's origin, and not more than once per cooldown, so
  // a frame that dies on arrival cannot spin.
  useEffect(() => {
    if (!lease) return;
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== lease.origin || event.source !== ref.current?.contentWindow) return;
      const where = expiredFramePath(event.data, id);
      if (where === null || Date.now() - lastMint.current < REMINT_COOLDOWN_MS) return;
      wanted.current = where;
      mint();
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [lease, id, mint]);

  // While the page is awake, keep the cookie alive even if nobody touches the app:
  // a quarter of its life apart, a credentialed no-cors fetch the bridge answers
  // 204 and, past half-life, with a fresh cookie. Opaque to us on purpose — there
  // is nothing to read, and a hidden page's timers do not run, which is what the
  // resume path above is for.
  useEffect(() => {
    if (!lease) return;
    const touch = () => {
      if (document.visibilityState !== 'visible' || !loaded.current) return;
      fetch(keepaliveUrl(lease, id), { mode: 'no-cors', credentials: 'include', cache: 'no-store' }).catch(() => {});
    };
    const timer = setInterval(touch, keepaliveIntervalMs(lease));
    return () => clearInterval(timer);
  }, [lease, id]);

  // Keep open apps in sync without reloading their frames and losing local state.
  // The initial URL covers first paint; the request/reply covers delayed hydration.
  const sendTheme = () => {
    const frame = ref.current?.contentWindow;
    if (lease && frame) sendAppTheme(frame, lease.origin);
  };
  useEffect(() => {
    const frame = ref.current?.contentWindow;
    if (lease && frame) return connectAppTheme(frame, lease.origin);
  }, [lease]);

  return (
    <div className="appframe" style={active ? undefined : { display: 'none' }}>
      {state === 'loading' && <div className="appframe-status">Loading {label}…</div>}
      {state === 'slow' && (
        <div className="appframe-status">Still loading {label}… a slow connection can take a while.</div>
      )}
      {state === 'error' && (
        <div className="appframe-status appframe-error">
          {label} isn’t responding. Check that its service is running; it is tried again when you come back.
        </div>
      )}
      {lease && (
        <iframe
          ref={ref}
          title={label}
          src={lease.src}
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
          // Connected apps have a separate origin, so Copy buttons need explicit
          // write permission. Restrict it to this frame's source; never grant reads.
          allow="clipboard-write 'src'"
          // Introduce the shell to its app. An embedded app can learn who its
          // shell is from its first document's referrer and address its route
          // reports (ava:navigation) there. A reverse proxy in front of Ava
          // commonly sends `Referrer-Policy: same-origin` on every page, Ava's
          // included; with apps.origin set the frame is cross-origin, so the
          // default left document.referrer empty, the app could not tell who its
          // shell was, and every refresh landed on its home page — a Machine
          // Learning tab lost to F5. This covers this frame's own navigation only
          // and sends Ava's origin, never its path or query (no referrer ever
          // carries a fragment). It is a hint, not a guarantee: a redirect on the
          // way in that carries its own Referrer-Policy empties it again, which
          // is why apps are told to prefer ancestorOrigins and the
          // ava:theme-request handshake (docs/CONNECTOR_SDK.md §3). The bridge
          // never reads Referer; its app proxy passes it through like any header.
          referrerPolicy="origin"
          onLoad={() => { loaded.current = true; setState('ready'); sendTheme(); }}
        />
      )}
    </div>
  );
}
