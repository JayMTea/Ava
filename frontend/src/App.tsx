import type { ComponentType } from 'react';
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import { ActionConsole } from './components/ActionConsole';
import { AppFrame } from './components/AppFrame';
import { ArtifactPanel } from './components/artifact/ArtifactPanel';
import { ChatView } from './components/chat/ChatView';
import { Composer } from './components/chat/Composer';
import { Lightbox } from './components/chat/Media';
import { Drawer } from './components/Drawer';
import { Header } from './components/Header';
import { ViewErrorBoundary } from './components/ViewErrorBoundary';
import { useBrandName } from './lib/brandContext';

const AgentView = lazy(() => import('./components/agent/AgentView'));
// Lazy: the feature is OFF by default, so the majority of installs should not
// pay for this chunk on first paint.
const DomainsView = lazy(() => import('./components/domains/DomainsView'));

import { Skeleton } from './components/ui/layout';
import { HardwareBubble } from './components/HardwareBubble';
import { HubView } from './components/hub/HubView';
import InferenceBanner from './components/InferenceBanner';
import TourHost from './components/tour/TourHost';
import { useChat } from './hooks/useChat';
import { api } from './lib/api';
import { registerApps } from './lib/appColor';
import { RAIL_REALMS_OFF, type RailRealms, railRealms } from './lib/realms';
import type { AppEntry, Artifact, Attachment } from './lib/types';

// A view id is one of the built-in tabs or an app id from /api/apps.
type View = string;

// Built-in tabs that ship in the shell (always present, no connector needed).
const BUILTIN_VIEWS = ['chat', 'hub', 'agent', 'domains'];

// Sidebar resize limits. The floor is set by the panel's own contents — the head
// row (wordmark + two icon buttons) and the "Settings & dashboards" foot row stop
// being readable below it, and a sidebar that ellipsises its own furniture is not
// a narrower sidebar, it is a broken one. The ceiling keeps the chat column
// (--col: 768px) off the composer's minimum on a 1280px laptop, which is the
// smallest screen anyone resizes this on.
const NAV_W_MIN = 240;
const NAV_W_MAX = 460;
const NAV_W_DEFAULT = 300;
const clampNav = (w: number) => Math.round(Math.max(NAV_W_MIN, Math.min(NAV_W_MAX, w)));

// Matches the 760px breakpoint every stylesheet in the repo uses. Module scope,
// not the component body: it closes over nothing, and as a per-render arrow it
// was a fresh identity each pass, so every useCallback that consulted it had to
// either take a dependency that changes every render — defeating the memo — or
// leave it out and be flagged for it.
const isMobile = () => typeof window !== 'undefined' && window.innerWidth <= 760;

// Registry of native app views. The core shell ships NONE — personal/first-party
// apps live in an optional, gitignored overlay (frontend/src/overlay/views/*),
// each module default-exporting a component and naming its key via `viewId`.
// A connector with `ui.embed: native` selects one via `ui.view`; if the matching
// overlay module is absent the shell shows a graceful "not bundled" placeholder.
// import.meta.glob returns {} when the overlay folder doesn't exist, so a fresh
// fork builds cleanly with no personal apps.
const _overlayViews = import.meta.glob('./overlay/views/*.{tsx,ts}', { eager: true }) as Record<
  string,
  { default?: ComponentType; viewId?: string }
>;
const NATIVE_VIEWS: Record<string, ComponentType> = {};
for (const [path, mod] of Object.entries(_overlayViews)) {
  if (!mod.default) continue;
  const key = mod.viewId || path.split('/').pop()!.replace(/\.(tsx|ts)$/, '');
  NATIVE_VIEWS[key] = mod.default;
}

// The active tab lives in the URL hash (#vitals, #ops, an app id, …) so it's bookmarkable and the
// browser back/forward buttons move between tabs. App ids are accepted
// optimistically (the /api/apps list confirms them once loaded).
function viewFromHash(): View | null {
  if (typeof window === 'undefined') return null;
  const h = window.location.hash.replace(/^#\/?/, '').split('/')[0];
  return h || null;
}

export default function App() {
  const chat = useChat();
  // Left-rail apps, derived server-side from connector `ui:` blocks. Loaded at
  // boot and re-fetched when the Hub connects/removes an app (ava:apps-changed)
  // — a new tile appears without a page refresh.
  const [apps, setApps] = useState<AppEntry[]>([]);
  useEffect(() => {
    const load = () => api.apps().then((r) => {
      setApps(r.apps);
      registerApps(r.apps); // lets any component attribute tools/URLs to an app
    }).catch(() => setApps([]));
    load();
    window.addEventListener('ava:apps-changed', load);
    return () => window.removeEventListener('ava:apps-changed', load);
  }, []);
  // Realm grouping for the rail, joined to the app list in the browser on
  // `surface.owner === app.id`. Deliberately NOT a field on /api/apps: the
  // module that knows the taxonomy already imports the connector registry, so
  // teaching the registry about the taxonomy would be a cycle — and it would
  // put a YAML parse behind the route that paints the sidebar on every boot.
  // A failed fetch leaves the rail ungrouped and the destination visible.
  const [realms, setRealms] = useState<RailRealms>(RAIL_REALMS_OFF);
  useEffect(() => {
    const load = () => api.domains()
      .then((c) => setRealms(railRealms(c)))
      .catch(() => setRealms(RAIL_REALMS_OFF));
    load();
    window.addEventListener('ava:apps-changed', load);
    return () => window.removeEventListener('ava:apps-changed', load);
  }, []);
  // Branding — from the context, which the pre-paint script in index.html has
  // already applied to the DOM. This used to be a local useState('Ava') plus its
  // own /api/brand fetch, which meant one guaranteed frame of "Ava" on every
  // load of a re-branded install, and a second identical fetch in HubView
  // because threading the value down as a prop was worse than duplicating it.
  const brand = useBrandName();
  // Active tab: the URL hash (bookmark/back-button), else Setup.
  //
  // Landing on Setup is deliberate. A user arriving with no hash has almost
  // always just installed, and Setup is where the walkthrough starts and where
  // everything is named.
  //
  // The old chain read localStorage['ava.view'] in the middle here, and the
  // first visit's default was written back immediately, so the landing tab
  // became a sticky preference the user had never chosen. That key is gone
  // entirely — nothing else consumed it, and leaving the write behind would
  // invite the read back.
  const [view, setView] = useState<View>(() => viewFromHash() || 'hub');
  // Reflect the view in the URL hash so Back/forward and bookmarks work. The
  // FIRST stamp replaces rather than pushes: a bare `/` is where the setup
  // wizard's `location.href='/'` lands, and pushing would leave `/` in history —
  // so Back would appear to do nothing, returning to a URL that resolves to the
  // same view and cannot re-write the hash. replaceState fires no hashchange, so
  // the listener below cannot loop.
  const stamped = useRef(false);
  useEffect(() => {
    if (viewFromHash() === view) { stamped.current = true; return; }
    if (!stamped.current) {
      stamped.current = true;
      window.history.replaceState(null, '', `#${view}`);
    } else {
      window.location.hash = view;
    }
  }, [view]);
  // Back/forward buttons (and manual hash edits / bookmarks) drive the view.
  useEffect(() => {
    const onHash = () => {
      const v = viewFromHash();
      if (v && v !== view) setView(v);
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, [view]);
  // Switch tabs with a smooth cross-fade where the browser supports the View
  // Transitions API (Chromium/Safari); elsewhere it just swaps. flushSync makes
  // React apply the view change synchronously inside the transition callback so
  // the API can snapshot before/after. Honors reduced-motion.
  const changeView = useCallback((v: View) => {
    const d = document as Document & { startViewTransition?: (cb: () => void) => void };
    const reduce = typeof window !== 'undefined'
      && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    if (v === view || typeof d.startViewTransition !== 'function' || reduce) { setView(v); return; }
    d.startViewTransition(() => flushSync(() => setView(v)));
  }, [view]);
  // The Agent tab's "Reply in Chats" hand-off (agent/Thread.tsx dispatches it).
  // ONE listener, here, because App owns both halves of the jump — the chat
  // store and hash segment 0 — and the Agent console deliberately has no
  // composer to answer from: Chats is the one place you talk to the agent.
  const openChatById = chat.openChat;
  useEffect(() => {
    const onOpen = (e: Event) => {
      const id = (e as CustomEvent<{ id?: string }>).detail?.id;
      if (!id) return;
      openChatById(id);
      setView('chat');
    };
    window.addEventListener('ava:open-chat', onOpen);
    return () => window.removeEventListener('ava:open-chat', onOpen);
  }, [openChatById]);
  // Iframe apps a user has opened this session. Visited frames stay mounted
  // (hidden, not unmounted) so switching to chat/settings and back doesn't
  // reload the app and wipe its in-page state — typed prompts, scroll,
  // running-job progress bars. Lazy: an app loads nothing until first opened.
  const [openedApps, setOpenedApps] = useState<string[]>([]);
  // Append-only, exactly like `openedApps`: the Agent view loads nothing until
  // first opened, and never unloads after.
  const [agentVisited, setAgentVisited] = useState(false);
  useEffect(() => { if (view === 'agent') setAgentVisited(true); }, [view]);

  useEffect(() => {
    if (BUILTIN_VIEWS.includes(view)) return;
    setOpenedApps((prev) => (prev.includes(view) ? prev : [...prev, view]));
  }, [view]);
  // Sidebar open/collapsed: remember the user's choice across reloads. On mobile
  // the sidebar is a full-screen overlay, so always start closed there and never
  // persist that transient state (it must not leak into the desktop preference).
  const [sidebarOpen, setSidebarOpen] = useState(() => {
    if (typeof window === 'undefined') return true;
    if (window.innerWidth <= 760) return false;
    const stored = localStorage.getItem('ava.sidebarOpen');
    return stored == null ? true : stored === '1';
  });
  useEffect(() => {
    if (typeof window === 'undefined' || window.innerWidth <= 760) return;
    try { localStorage.setItem('ava.sidebarOpen', sidebarOpen ? '1' : '0'); } catch { /* storage unavailable */ }
  }, [sidebarOpen]);
  // Sidebar width. Resizable, but inside limits — a sidebar narrower than its
  // own rows is not a preference, and one wide enough to squeeze the chat column
  // costs more than it gives. Remembered across reloads like the open/closed
  // state, and ignored on mobile, where the drawer is a full-height overlay at a
  // fixed width and the handle is not rendered.
  const [navWidth, setNavWidth] = useState(() => {
    if (typeof window === 'undefined') return NAV_W_DEFAULT;
    const stored = Number(localStorage.getItem('ava.sidebarWidth'));
    return Number.isFinite(stored) && stored > 0 ? clampNav(stored) : NAV_W_DEFAULT;
  });
  const [navResizing, setNavResizing] = useState(false);
  useEffect(() => {
    try { localStorage.setItem('ava.sidebarWidth', String(navWidth)); } catch { /* storage unavailable */ }
  }, [navWidth]);
  const [text, setText] = useState('');
  const [artWidth, setArtWidth] = useState('50%');
  const [refreshing, setRefreshing] = useState(false);
  const [lightbox, setLightbox] = useState<string | null>(null);


  const shellRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);

  const openLightbox = useCallback((url: string) => setLightbox(url), []);
  const closeLightbox = useCallback(() => setLightbox(null), []);

  // ---- sidebar drag resize (desktop) ---------------------------------------
  // Same shape as the artifact divider below, with two differences that matter:
  // the width is absolute px rather than a percentage (a sidebar's job is to fit
  // its rows, not to hold a share of the window), and `navResizing` is REACT
  // STATE rather than a class poked onto the node. Poking it would not survive —
  // every drag frame calls setNavWidth, and the re-render rewrites className from
  // props, dropping the class and letting the .22s width transition back in to
  // lag the pointer for the rest of the drag.
  const navDragging = useRef(false);
  const startNavDrag = useCallback((e: React.MouseEvent | React.TouchEvent) => {
    if (isMobile()) return;
    navDragging.current = true;
    setNavResizing(true);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
    const move = (ev: MouseEvent | TouchEvent) => {
      if (!navDragging.current) return;
      const x = 'touches' in ev ? ev.touches[0]?.clientX : ev.clientX;
      if (x == null) return;
      // Measured from the shell's left edge, not the viewport's: the shell is
      // the drawer's containing block, and they are not the same origin once
      // anything sits to the left of it.
      const left = shellRef.current?.getBoundingClientRect().left ?? 0;
      setNavWidth(clampNav(x - left));
    };
    const stop = () => {
      navDragging.current = false;
      setNavResizing(false);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', stop);
      window.removeEventListener('touchmove', move);
      window.removeEventListener('touchend', stop);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', stop);
    window.addEventListener('touchmove', move, { passive: false });
    window.addEventListener('touchend', stop);
  }, []);
  // A drag handle is functionality, so it answers to the keyboard too — the
  // separator pattern: arrows nudge, Shift jumps, Home/End take the limits, and
  // Enter restores the default rather than making "put it back" a pixel hunt.
  const onNavKey = useCallback((e: React.KeyboardEvent) => {
    const step = e.shiftKey ? 48 : 16;
    switch (e.key) {
      case 'ArrowLeft': e.preventDefault(); setNavWidth((w) => clampNav(w - step)); break;
      case 'ArrowRight': e.preventDefault(); setNavWidth((w) => clampNav(w + step)); break;
      case 'Home': e.preventDefault(); setNavWidth(NAV_W_MIN); break;
      case 'End': e.preventDefault(); setNavWidth(NAV_W_MAX); break;
      case 'Enter': case ' ': e.preventDefault(); setNavWidth(NAV_W_DEFAULT); break;
      default: break;
    }
  }, []);

  // ---- divider drag resize (desktop) --------------------------------------
  const onMove = useCallback((clientX: number) => {
    const shell = shellRef.current;
    if (!shell) return;
    const rect = shell.getBoundingClientRect();
    let pct = ((rect.right - clientX) / rect.width) * 100;
    pct = Math.max(24, Math.min(78, pct));
    setArtWidth(pct.toFixed(1) + '%');
  }, []);
  const startDrag = useCallback(
    (e: React.MouseEvent | React.TouchEvent) => {
      dragging.current = true;
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      e.preventDefault();
      const move = (ev: MouseEvent | TouchEvent) => {
        if (!dragging.current) return;
        const x = 'touches' in ev ? ev.touches[0]?.clientX : ev.clientX;
        if (x != null) onMove(x);
      };
      const stop = () => {
        dragging.current = false;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        window.removeEventListener('mousemove', move);
        window.removeEventListener('mouseup', stop);
        window.removeEventListener('touchmove', move);
        window.removeEventListener('touchend', stop);
      };
      window.addEventListener('mousemove', move);
      window.addEventListener('mouseup', stop);
      window.addEventListener('touchmove', move, { passive: false });
      window.addEventListener('touchend', stop);
    },
    [onMove],
  );

  const onSend = useCallback(() => {
    const t = text;
    setText('');
    chat.send(t);
  }, [text, chat]);

  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    await chat.refreshArtifact();
    setRefreshing(false);
  }, [chat]);

  const openArtifact = useCallback((a: Artifact) => chat.setArtifact(a), [chat]);
  const retryUser = useCallback(
    (t: string, atts: Attachment[], id: string) => chat.retry(t, atts, id),
    [chat],
  );
  const retryAva = useCallback((t: string, atts: Attachment[]) => chat.retry(t, atts, null), [chat]);

  return (
    <>
      <div
        id="appShell"
        className={
          (chat.artifact ? 'art-open' : '') +
          (sidebarOpen ? ' nav-open' : ' nav-closed') +
          (navResizing ? ' nav-drag' : '')
        }
        style={{ ['--art-w' as string]: artWidth, ['--nav-w' as string]: `${navWidth}px` }}
        ref={shellRef}
      >
        <Drawer
          realms={realms}
          open={sidebarOpen}
          onToggle={() => setSidebarOpen((o) => !o)}
          apps={apps}
          brand={brand}
          view={view}
          onView={(v) => {
            changeView(v);
            if (isMobile()) setSidebarOpen(false);
          }}
          chats={chat.chats}
          currentChatId={chat.currentChatId}
          onNewChat={() => {
            chat.newChat();
            setView('chat');
            if (isMobile()) setSidebarOpen(false);
          }}
          onOpenChat={(id) => {
            chat.openChat(id);
            setView('chat');
            if (isMobile()) setSidebarOpen(false);
          }}
          onDeleteChat={chat.deleteChat}
        />

        {/* Sidebar resize handle. Rendered always and shown by CSS only when the
            panel is expanded and we are not on a phone — the same arrangement as
            #artifactDivider, so the DOM does not shuffle on every collapse. */}
        <div
          id="navDivider"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize sidebar"
          aria-valuenow={navWidth}
          aria-valuemin={NAV_W_MIN}
          aria-valuemax={NAV_W_MAX}
          tabIndex={0}
          title="Drag to resize — double-click to reset"
          onMouseDown={startNavDrag}
          onTouchStart={startNavDrag}
          onDoubleClick={() => setNavWidth(NAV_W_DEFAULT)}
          onKeyDown={onNavKey}
        >
          <span className="nav-grip" />
        </div>

        <div id="appCol">
          <Header status={chat.status} onMenu={() => setSidebarOpen((o) => !o)} ghost={chat.ghost} onToggleGhost={chat.toggleGhost} showGhost={view === 'chat'} models={chat.models} model={chat.model} agentModel={chat.agentModel} onSetModel={chat.setModelMode} />
          <div id="viewPort">
            {/* Suspense INSIDE the boundary: a chunk that fails to load is a view
                error the boundary should own, not a blank screen. */}
            {view === 'domains' && (
              <ViewErrorBoundary label="Domains">
                <Suspense fallback={<Skeleton height={260} />}><DomainsView /></Suspense>
              </ViewErrorBoundary>
            )}
            {/* Agent follows the AppFrame pattern, not the ChatView one: it is
                KEPT MOUNTED once visited and merely hidden. Unmounting would
                kill the terminal's PTY and the browser panel's snapshot state —
                the same reason iframe apps stay in the tree below. The cost is
                that its error state no longer self-clears on tab switch, which
                ViewErrorBoundary's epoch-keyed "Try again" already covers. */}
            {agentVisited && (
              <ViewErrorBoundary label="Agent" hidden={view !== 'agent'}>
                <Suspense fallback={<Skeleton height={260} />}>
                  <AgentView active={view === 'agent'} />
                </Suspense>
              </ViewErrorBoundary>
            )}
            {view === 'hub' && <ViewErrorBoundary label="Setup"><HubView /></ViewErrorBoundary>}
            {view === 'chat' && (
              <ViewErrorBoundary label="Chat">
                <ChatView
                  items={chat.items}
                  currentChatId={chat.currentChatId}
                  onRetryUser={retryUser}
                  onRetryAva={retryAva}
                  onReplay={chat.replay}
                  onQuickSay={chat.quickSay}
                  onOpenLightbox={openLightbox}
                  onOpenArtifact={openArtifact}
                />
              </ViewErrorBoundary>
            )}
            {/* Kept-alive iframe apps: every visited frame stays in the tree;
                only the active one is shown. Unmounting would reload the app
                and lose whatever the user had in progress inside it. */}
            {apps
              .filter((a) => a.embed === 'iframe' && openedApps.includes(a.id))
              .map((a) => (
                <ViewErrorBoundary key={a.id} label={a.label} hidden={view !== a.id}>
                  <AppFrame id={a.id} label={a.label} active={view === a.id} />
                </ViewErrorBoundary>
              ))}
            {!BUILTIN_VIEWS.includes(view) && (() => {
              const app = apps.find((a) => a.id === view);
              if (!app) return null; // apps still loading, or unknown id
              if (app.embed === 'native') {
                const Cmp = NATIVE_VIEWS[app.view || app.id];
                return Cmp ? (
                  <ViewErrorBoundary label={app.label}><Cmp /></ViewErrorBoundary>
                ) : (
                  <div className="panel-empty">Native view “{app.view || app.id}” is not bundled.</div>
                );
              }
              if (app.embed === 'iframe') return null; // rendered by the kept-alive block above
              return <ViewErrorBoundary label={app.label}><ActionConsole id={app.id} label={app.label} /></ViewErrorBoundary>;
            })()}
          </div>
          {view === 'chat' && (
            <Composer
              text={text}
              onText={setText}
              pending={chat.pending}
              onRemoveAtt={chat.removeAtt}
              onFiles={chat.uploadFiles}
              onSend={onSend}
              onTalk={chat.talk}
              onStop={chat.canStop ? chat.stop : undefined}
              busy={chat.busy}
              hint={chat.hint}
              ctxTokens={chat.ctxTokens}
              ctxMax={chat.ctxMax}
            />
          )}
        </div>

        <div id="artifactDivider" title="Drag to resize" onMouseDown={startDrag} onTouchStart={startDrag}>
          <span className="art-grip" />
        </div>

        <ArtifactPanel
          artifact={chat.artifact}
          onClose={() => chat.setArtifact(null)}
          onRefresh={onRefresh}
          refreshing={refreshing}
        />
      </div>

      {/* biome-ignore lint/a11y/useKeyWithClickEvents: a decorative backdrop, not
          an affordance. Keyboard users close the sidebar with its own toggle
          button, which stays in the tab order; making the scrim focusable would
          add a tab stop that announces nothing. aria-hidden keeps it out of the
          accessibility tree entirely. */}
      <div
        id="scrim"
        aria-hidden="true"
        className={sidebarOpen ? 'open' : ''}
        onClick={() => setSidebarOpen(false)}
      />

      {lightbox && <Lightbox url={lightbox} onClose={closeLightbox} />}

      {/* Wrapped like every other view. It was the one top-level component
          without a boundary, which was survivable while it only rendered numbers
          — now that it carries an actuator, a throw in there would take the whole
          app down rather than one floating widget. */}
      <ViewErrorBoundary label="Hardware monitor"><HardwareBubble /></ViewErrorBoundary>
      <InferenceBanner />
      <TourHost view={view} />
    </>
  );
}
