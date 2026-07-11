import { useCallback, useEffect, useRef, useState } from 'react';
import type { ComponentType } from 'react';
import { Drawer } from './components/Drawer';
import { Header } from './components/Header';
import { Composer } from './components/chat/Composer';
import { ChatView } from './components/chat/ChatView';
import { ArtifactPanel } from './components/artifact/ArtifactPanel';
import { Lightbox } from './components/chat/Media';
import { AppFrame } from './components/AppFrame';
import { ViewErrorBoundary } from './components/ViewErrorBoundary';
import { ActionConsole } from './components/ActionConsole';
import { VitalsView } from './components/dashboard/VitalsView';
import { OpsView } from './components/dashboard/OpsView';
import { HubView } from './components/hub/HubView';
import { HardwareBubble } from './components/HardwareBubble';
import { useChat } from './hooks/useChat';
import { api } from './lib/api';
import type { AppEntry, Artifact, Attachment } from './lib/types';

// A view id is one of the built-in tabs or an app id from /api/apps.
type View = string;

// Built-in tabs that ship in the shell (always present, no connector needed).
const BUILTIN_VIEWS = ['vitals', 'ops', 'chat', 'hub'];

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
    const load = () => api.apps().then((r) => setApps(r.apps)).catch(() => setApps([]));
    load();
    window.addEventListener('ava:apps-changed', load);
    return () => window.removeEventListener('ava:apps-changed', load);
  }, []);
  // Branding (name/tagline) — config-driven so a fork re-brands with no code edits.
  const [brand, setBrand] = useState('Ava');
  useEffect(() => {
    api.brand().then((b) => {
      if (b?.name) {
        setBrand(b.name);
        document.title = b.name;
      }
    }).catch(() => { /* keep default */ });
  }, []);
  // Active tab: prefer the URL hash (bookmark/back-button), then the last-used
  // tab from localStorage, else the vitals home.
  const [view, setView] = useState<View>(() => {
    const fromHash = viewFromHash();
    if (fromHash) return fromHash;
    if (typeof window === 'undefined') return 'vitals';
    return localStorage.getItem('ava.view') || 'vitals';
  });
  // Reflect the view in the URL hash (creates a history entry so Back works) and
  // remember it for the next visit.
  useEffect(() => {
    try { localStorage.setItem('ava.view', view); } catch { /* storage unavailable */ }
    if (viewFromHash() !== view) window.location.hash = view;
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
  // Iframe apps a user has opened this session. Visited frames stay mounted
  // (hidden, not unmounted) so switching to chat/settings and back doesn't
  // reload the app and wipe its in-page state — typed prompts, scroll,
  // running-job progress bars. Lazy: an app loads nothing until first opened.
  const [openedApps, setOpenedApps] = useState<string[]>([]);
  useEffect(() => {
    if (BUILTIN_VIEWS.includes(view)) return;
    setOpenedApps((prev) => (prev.includes(view) ? prev : [...prev, view]));
  }, [view]);
  const [sidebarOpen, setSidebarOpen] = useState(() => (typeof window !== 'undefined' ? window.innerWidth > 760 : true));
  const [text, setText] = useState('');
  const [codeMode, setCodeMode] = useState(false);
  const [artWidth, setArtWidth] = useState('50%');
  const [refreshing, setRefreshing] = useState(false);
  const [lightbox, setLightbox] = useState<{ url: string; onClose?: () => void } | null>(null);

  const isMobile = () => typeof window !== 'undefined' && window.innerWidth <= 760;

  const shellRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);

  const openLightbox = useCallback((url: string, onClose?: () => void) => setLightbox({ url, onClose }), []);
  const closeLightbox = useCallback(() => {
    setLightbox((lb) => {
      lb?.onClose?.();
      return null;
    });
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
    chat.send(t, codeMode);
  }, [text, codeMode, chat]);

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
        className={(chat.artifact ? 'art-open' : '') + (sidebarOpen ? ' nav-open' : ' nav-closed')}
        style={{ ['--art-w' as string]: artWidth }}
        ref={shellRef}
      >
        <Drawer
          open={sidebarOpen}
          onToggle={() => setSidebarOpen((o) => !o)}
          apps={apps}
          brand={brand}
          view={view}
          onView={(v) => {
            setView(v);
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

        <div id="appCol">
          <Header status={chat.status} onMenu={() => setSidebarOpen((o) => !o)} ghost={chat.ghost} onToggleGhost={chat.toggleGhost} showGhost={view === 'chat'} brand={brand} models={chat.models} model={chat.model} onSetModel={chat.setModelMode} />
          <div id="viewPort">
            {view === 'vitals' && <ViewErrorBoundary label="Vitals"><VitalsView /></ViewErrorBoundary>}
            {view === 'ops' && <ViewErrorBoundary label="Operations"><OpsView /></ViewErrorBoundary>}
            {view === 'hub' && <ViewErrorBoundary label="Setup"><HubView /></ViewErrorBoundary>}
            {view === 'chat' && (
              <ViewErrorBoundary label="Chat">
                <ChatView
                  items={chat.items}
                  currentChatId={chat.currentChatId}
                  onCancelGen={chat.cancelGen}
                  onRetryUser={retryUser}
                  onRetryAva={retryAva}
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
              busy={chat.busy}
              codeMode={codeMode}
              onToggleCode={setCodeMode}
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

      <div id="scrim" className={sidebarOpen ? 'open' : ''} onClick={() => setSidebarOpen(false)} />

      {lightbox && <Lightbox url={lightbox.url} onClose={closeLightbox} />}

      <HardwareBubble />
    </>
  );
}
