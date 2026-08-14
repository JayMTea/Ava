import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
  RefObject,
} from 'react';
import { createPortal } from 'react-dom';
import type { AppEntry, ChatSummary } from '../lib/types';
import { Icon } from '../lib/icons';
import { AppDot, appAccent, appIcon } from '../lib/appColor';
import { DEFAULT_BRAND } from '../lib/brand';
import { useBrand } from '../lib/brandContext';
import { type Placed, type Viewport, placePanel, placeRail } from './flyoutPlace';
import { ConnectAppDialog } from './hub/ConnectApp';

// Ava's own wordmark, shipped under the SPA's static mount. Rendered only when
// this install still answers to Ava's name: the lockup spells "AVA", so an
// install renamed to something else must fall back to its own name as text
// rather than wearing a word it is not called. An owner who wants a lockup for
// their name uploads one — that is what the wordmark slot is for.
const AVA_WORDMARK = {
  dark: '/assets/marks/ava-wordmark.png',        // light ink, for the dark canvas
  light: '/assets/marks/ava-wordmark-light.png', // dark ink, for the light canvas
};

// ── Sidebar destinations ────────────────────────────────────────────────────
// One list, three consumers: the expanded panel's inline rows, the collapsed
// rail's foot flyout, and the expanded panel's foot flyout. `system: true` marks
// the dashboards-and-settings destinations, which are inline nowhere — they live
// behind the "Settings & dashboards" pop-up at the foot of whichever sidebar
// form is on screen, so the panel stays the thing you are doing: chats, your
// apps, your history. Everything else in the sidebar is derived from the
// connector app registry (/api/apps), so a new app appears by dropping a
// connector folder — no edits here.
//
// KEEP THIS ONE LITERAL, AND KEEP ITS SHAPE. tests/test_icon_ssot.py parses this
// declaration out of the file and asserts every icon name exists in
// lib/icons.tsx. The rail flyout used to carry a second, inline copy of the four
// system entries, which that guard could never see — a typo there rendered an
// empty slot and failed nothing. What the guard needs: `id` first inside the
// braces and `icon` before the closing brace, both single-quoted; no equals sign
// in the type annotation; no `as const`; and the array closed by a bare
// bracket-semicolon alone on its own line.
type NavItem = { id: string; label: string; icon: string; system?: boolean };
const NAV: NavItem[] = [
  { id: 'chat', label: 'Chats', icon: 'chats' },
  { id: 'vitals', label: 'Vitals', icon: 'gauge', system: true },
  { id: 'ops', label: 'Operations', icon: 'activity', system: true },
  { id: 'data', label: 'Data', icon: 'db', system: true },
  { id: 'hub', label: 'Setup', icon: 'sliders', system: true },
];
const NAV_INLINE = NAV.filter((it) => !it.system);
const NAV_SYSTEM = NAV.filter((it) => it.system);

/** The sidebar head: a wordmark image when there is one to show, else the name
 *  as text — which is what this always did before the slot had a default.
 *
 *  BOTH variants are rendered and CSS picks one from `data-theme`, rather than
 *  subscribing to the theme here. A wordmark is ink on transparent, so it needs
 *  one file per canvas, and the theme is already stamped on <html> before first
 *  paint — reading it in React would repaint after the stamp, not with it.
 *
 *  An owner who fills only `wordmark` gets it on both canvases. That mirrors how
 *  `accent_light` already behaves (blank uses `accent` in both) rather than
 *  inventing a second rule for the same shape of problem. */
function BrandWord({ name }: { name: string }) {
  const { assets } = useBrand();
  const own = assets?.wordmark || '';
  const ownLight = assets?.wordmark_light || own;
  const isAva = name === DEFAULT_BRAND.name;

  const dark = own || (isAva ? AVA_WORDMARK.dark : '');
  const light = ownLight || (isAva ? AVA_WORDMARK.light : '');

  if (!dark) return <span className="brand-word">{name}</span>;
  return (
    <span className="brand-word">
      <img className="brand-wm brand-wm-dark" src={dark} alt={name} />
      <img className="brand-wm brand-wm-light" src={light} alt="" aria-hidden="true" />
    </span>
  );
}

// ── The "Settings & dashboards" flyout ──────────────────────────────────────
// Two triggers, one behaviour. The collapsed rail's foot has an icon button that
// opens on hover with a grace timer; the expanded panel's foot has a full-width
// labelled row that opens on CLICK ONLY — a 284px bar across the bottom of the
// panel is on the way to everything, so hover-to-open would fire every time the
// pointer travelled down toward Recents. A 40px icon in a 56px rail is the
// opposite: reaching it is already a deliberate visit.
//
// Everything after "it is open" is identical, so it lives here once — including
// the part that was missing.
//
// THE KEYBOARD IS NOT OPTIONAL. A portal renders at the END of <body>, so Tab
// from the trigger does not enter the menu. Those four destinations used to be
// plain tabbable <button>s in the panel's nav list; moving them behind a
// keyboard-dead pop-up would make Vitals / Operations / Data / Setup unreachable
// by keyboard in BOTH sidebar forms. That is WCAG 2.1.1 Level A, and it is the
// same defect the Recents rows below carry a comment about. The rail flyout has
// shipped with this gap since it was written, and only got away with it because
// the same four rows were still inline in the expanded panel — which is the
// escape hatch this change removes. The hook closes it for both.
//
// No first-letter typeahead: optional in WAI-ARIA, and there are four items.

interface FlyoutProps {
  items: NavItem[];
  view: string;
  onView: (v: string) => void;
  /** The sidebar's open/collapsed state. Not used for layout — used to DISMISS.
   *  The menu is portalled to <body>, so it outlives its trigger: collapsing the
   *  panel sets `.side-panel { display:none }` and the menu would keep floating
   *  over the chat canvas, anchored to a button that is no longer there. */
  drawerOpen: boolean;
}

function useFlyout(
  count: number,
  place: (r: DOMRect, vp: Viewport) => Placed,
  drawerOpen: boolean,
) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<Placed>({ left: 0, bottom: 0 });
  // Roving focus index. -1 means "no item owns focus" — the correct state for a
  // hover-opened menu: hover must never pull focus out from under whatever the
  // user is actually typing in.
  const [cursor, setCursor] = useState(-1);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const closeTimer = useRef<number | undefined>(undefined);
  // Escape dismissed it while the pointer was still on the trigger. Without this
  // the menu reappears instantly: unmounting the portal makes the browser redo
  // hit-testing, React sees a fresh mouseover on the still-hovered trigger and
  // fires onMouseEnter again, so Escape looked like it did nothing at all.
  // Cleared when the pointer leaves, or on any deliberate re-open.
  const escaped = useRef(false);

  const measure = useCallback(() => {
    const r = triggerRef.current?.getBoundingClientRect();
    if (r) setPos(place(r, { width: window.innerWidth, height: window.innerHeight }));
  }, [place]);

  /** `focus` = which item takes focus once the portal exists, or null to open
   *  without moving focus (hover). Re-entering an already-open menu passes null,
   *  so the roving cursor is never reset out from under the keyboard. */
  const openMenu = useCallback((focus: number | null) => {
    window.clearTimeout(closeTimer.current);
    escaped.current = false;
    measure();
    setOpen(true);
    if (focus != null) setCursor(focus);
  }, [measure]);

  // Hover-open defers to a preceding Escape. The dismissal expires in
  // scheduleClose() below — i.e. as soon as the pointer leaves.
  const hoverOpen = useCallback(() => {
    if (!escaped.current) openMenu(null);
  }, [openMenu]);

  // close() leaves focus where it is (outside click, hover-out, sidebar
  // collapse). closeToTrigger() hands it back — required after Escape, and after
  // Tab so the browser continues the tab sequence from the TRIGGER rather than
  // from the end of <body>, where the portal lives.
  const close = useCallback(() => {
    window.clearTimeout(closeTimer.current);
    setOpen(false);
    setCursor(-1);
  }, []);
  const closeToTrigger = useCallback(() => {
    close();
    triggerRef.current?.focus();
  }, [close]);
  const scheduleClose = useCallback(() => {
    escaped.current = false;
    closeTimer.current = window.setTimeout(() => { setOpen(false); setCursor(-1); }, 140);
  }, []);

  // The sidebar swapped forms while the menu was open — see FlyoutProps. This is
  // the render-phase adjustment React documents for "reset state when a prop
  // changes", NOT an effect: an effect runs after paint, so the menu would show
  // for a frame hanging off a trigger that is already display:none. Any pending
  // close timer is left alone on purpose — if it fires it repeats this, which is
  // a no-op.
  const [lastDrawerOpen, setLastDrawerOpen] = useState(drawerOpen);
  if (lastDrawerOpen !== drawerOpen) {
    setLastDrawerOpen(drawerOpen);
    setOpen(false);
    setCursor(-1);
  }

  // One place moves focus: the cursor changes, this puts focus on it. Layout
  // effect, not effect — the portal is measured, painted and focused in the same
  // frame, so focus never touches <body> in between.
  useLayoutEffect(() => {
    if (open && cursor >= 0) itemRefs.current[cursor]?.focus();
  }, [open, cursor]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (triggerRef.current?.contains(t) || menuRef.current?.contains(t)) return;
      close();
    };
    // Escape lives on the document, not on the menu, because a hover-opened menu
    // has no focus inside it — a menu-scoped handler would leave the rail's
    // flyout undismissable by keyboard, which is what it did before this line.
    // Capture phase so stopPropagation actually lands: React attaches its
    // listeners at the root container, BELOW document, so a bubble-phase
    // stopPropagation here would fire too late to keep this Escape out of the
    // panel's search field.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      escaped.current = true;
      // Hand focus back only if the menu had it. Hover never took focus, so
      // pulling it to the trigger would move the caret out of whatever the user
      // was actually typing in.
      if (menuRef.current?.contains(document.activeElement)) closeToTrigger();
      else close();
    };
    document.addEventListener('keydown', onKey, true);
    // Re-place, do not dismiss. RowMenu closes on scroll because its kebab rides
    // a scrolling list; both of these triggers are pinned to a sidebar that never
    // scrolls, so closing here would be a FALSE dismissal — the menu the user
    // just opened vanishing because they nudged the Recents list underneath it.
    document.addEventListener('pointerdown', onDown, true);
    window.addEventListener('scroll', measure, true);
    window.addEventListener('resize', measure);
    return () => {
      document.removeEventListener('keydown', onKey, true);
      document.removeEventListener('pointerdown', onDown, true);
      window.removeEventListener('scroll', measure, true);
      window.removeEventListener('resize', measure);
    };
  }, [open, close, closeToTrigger, measure]);

  const onMenuKeyDown = (e: ReactKeyboardEvent) => {
    const found = itemRefs.current.findIndex((el) => el === document.activeElement);
    const at = found < 0 ? 0 : found;
    const to = (i: number) => setCursor(((i % count) + count) % count);
    switch (e.key) {
      case 'ArrowDown': e.preventDefault(); to(at + 1); break;
      case 'ArrowUp': e.preventDefault(); to(at - 1); break;
      case 'Home': e.preventDefault(); to(0); break;
      case 'End': e.preventDefault(); to(count - 1); break;
      // Escape is NOT here — it is a document capture listener above, so it also
      // dismisses a hover-opened menu that never took focus.
      //
      // Deliberately NOT prevented. Focus goes back to the trigger synchronously,
      // then the browser's own Tab runs its default action from there — forwards
      // or backwards — exactly as if the menu had never opened. React unmounts
      // the portal afterwards, so nothing is left holding focus.
      case 'Tab': closeToTrigger(); break;
      default: break;
    }
  };

  // Enter and Space are deliberately absent: a <button> already turns both into
  // a click, and handling them here as well would open the menu on keydown and
  // close it again on the click that follows. The arrows open with focus placed
  // the way the menu READS — it grows upward, so Up lands on the item nearest
  // the trigger (the last one) and Down on the far one.
  const onTriggerKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key === 'ArrowUp') { e.preventDefault(); openMenu(count - 1); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); openMenu(0); }
  };

  return { open, pos, cursor, triggerRef, menuRef, itemRefs,
           openMenu, hoverOpen, close, closeToTrigger, scheduleClose,
           onMenuKeyDown, onTriggerKeyDown };
}

/** The menu itself — identical for both triggers, which is the point. */
function FlyoutMenu({
  id, labelledBy, className, items, view, onView,
  cursor, menuRef, itemRefs, style, onKeyDown, onClose, onMouseEnter, onMouseLeave,
}: {
  id: string; labelledBy: string; className?: string;
  items: NavItem[]; view: string; onView: (v: string) => void;
  cursor: number;
  menuRef: RefObject<HTMLDivElement | null>;
  itemRefs: RefObject<(HTMLButtonElement | null)[]>;
  style: CSSProperties;
  onKeyDown: (e: ReactKeyboardEvent) => void;
  onClose: () => void;
  onMouseEnter?: () => void; onMouseLeave?: () => void;
}) {
  return createPortal(
    <div
      id={id}
      ref={menuRef}
      className={'rail-menu' + (className ? ' ' + className : '')}
      role="menu"
      aria-orientation="vertical"
      aria-labelledby={labelledBy}
      style={style}
      onKeyDown={onKeyDown}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
    >
      {items.map((it, i) => (
        <button type="button"
          key={it.id}
          // Braces, not a concise body: React reads a ref callback's RETURN
          // value as a cleanup function.
          ref={(el) => { itemRefs.current[i] = el; }}
          role="menuitem"
          // Roving tabindex. Inside role="menu" exactly one item is in the tab
          // order and arrows do the rest. -1 across the board while cursor is -1
          // is correct for a hover-opened menu: Tab should leave the trigger the
          // way it always did.
          tabIndex={i === cursor ? 0 : -1}
          aria-current={view === it.id ? 'page' : undefined}
          className={'rail-menu-item' + (view === it.id ? ' active' : '')}
          onClick={() => { onView(it.id); onClose(); }}
        >
          <Icon name={it.icon} />
          <span>{it.label}</span>
        </button>
      ))}
    </div>,
    document.body,
  );
}

/** Collapsed rail, foot. Hover-opens with a 140ms grace timer covering the
 *  diagonal from button to menu — unchanged behaviour, now also operable from
 *  the keyboard. */
function RailFlyout({ items, view, onView, drawerOpen }: FlyoutProps) {
  const menuId = useId();
  const btnId = useId();
  const f = useFlyout(items.length, placeRail, drawerOpen);
  const active = items.some((it) => it.id === view);
  return (
    <div className="rail-menu-wrap"
      onMouseEnter={f.hoverOpen}
      onMouseLeave={f.scheduleClose}
    >
      <button type="button"
        id={btnId}
        ref={f.triggerRef}
        className={'rail-btn' + (active ? ' active' : '')}
        aria-haspopup="menu"
        aria-expanded={f.open}
        aria-controls={f.open ? menuId : undefined}
        title="Settings & dashboards"
        aria-label="Settings & dashboards"
        onClick={() => (f.open ? f.close() : f.openMenu(0))}
        onKeyDown={f.onTriggerKeyDown}
      >
        <Icon name="sliders" />
      </button>
      {f.open && (
        <FlyoutMenu
          id={menuId} labelledBy={btnId}
          items={items} view={view} onView={onView}
          cursor={f.cursor} menuRef={f.menuRef} itemRefs={f.itemRefs}
          style={f.pos} onKeyDown={f.onMenuKeyDown} onClose={f.closeToTrigger}
          onMouseEnter={f.hoverOpen} onMouseLeave={f.scheduleClose}
        />
      )}
    </div>
  );
}

/** Expanded panel, foot. No aria-label: the row's own text names it, and adding
 *  one would make `[aria-label="Settings & dashboards"]` match two elements —
 *  one of them display:none — which is how demo/src/tour.ts drives the rail's
 *  gear, and a two-element match is a Playwright strict-mode failure. */
function PanelFlyout({ items, view, onView, drawerOpen }: FlyoutProps) {
  const menuId = useId();
  const btnId = useId();
  const f = useFlyout(items.length, placePanel, drawerOpen);
  const active = items.some((it) => it.id === view);
  return (
    <>
      <button type="button"
        id={btnId}
        ref={f.triggerRef}
        className={'nav-item panel-foot-btn' + (active ? ' active' : '')}
        aria-haspopup="menu"
        aria-expanded={f.open}
        aria-controls={f.open ? menuId : undefined}
        onClick={() => (f.open ? f.close() : f.openMenu(0))}
        onKeyDown={f.onTriggerKeyDown}
      >
        <Icon name="sliders" className="nav-ic" />
        <span>Settings &amp; dashboards</span>
        <Icon name="chevronDown" className="panel-foot-chev" />
      </button>
      {f.open && (
        <FlyoutMenu
          id={menuId} labelledBy={btnId} className="panel-menu"
          items={items} view={view} onView={onView}
          cursor={f.cursor} menuRef={f.menuRef} itemRefs={f.itemRefs}
          style={f.pos} onKeyDown={f.onMenuKeyDown} onClose={f.closeToTrigger}
        />
      )}
    </>
  );
}

interface Props {
  open: boolean;
  onToggle: () => void;
  apps: AppEntry[];
  brand?: string;
  view: string;
  onView: (v: string) => void;
  chats: ChatSummary[];
  currentChatId: string | null;
  onNewChat: () => void;
  onOpenChat: (id: string) => void;
  onDeleteChat: (id: string) => void;
}

export function Drawer({
  open,
  onToggle,
  apps,
  brand = 'Ava',
  view,
  onView,
  chats,
  currentChatId,
  onNewChat,
  onOpenChat,
  onDeleteChat,
}: Props) {
  // Hover tooltip for the rail icons (claude.ai style). One shared label,
  // portalled to <body> — the drawer clips overflow — and fixed at the rail's
  // right edge, vertically centered on the hovered button.
  const [tip, setTip] = useState<{ label: string; top: number } | null>(null);
  // Expanded-panel search: the magnifier in the head row reveals an input that
  // filters the Recents list by title.
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  // "Connect your app" — the connect flow in a dialog, mounted from the Apps
  // section. Setup → Connectors is still where connected apps are managed; this
  // only removes the "…Setup, then Connectors, then the button" detour from the
  // one step an owner with no apps has to take first. The dialog is the shared
  // <ConnectAppFields> (hub/ConnectApp.tsx), not a sidebar-local copy.
  const [connectOpen, setConnectOpen] = useState(false);
  const tipProps = (label: string) => ({
    onMouseEnter: (e: ReactMouseEvent<HTMLElement>) => {
      const r = e.currentTarget.getBoundingClientRect();
      setTip({ label, top: r.top + r.height / 2 });
    },
    onMouseLeave: () => setTip(null),
  });

  const userApps = apps.filter((a) => a.section !== 'core');
  const q = query.trim().toLowerCase();
  const visibleChats = q
    ? chats.filter((c) => (c.title || 'New chat').toLowerCase().includes(q))
    : chats;

  // `accent` marks connected-app tabs with the app's identity color (a small
  // corner dot), so app destinations read as the app's, not Ava's.
  const railBtn = (id: string, label: string, icon: string, accent?: string) => (
    <button
      key={id}
      className={'rail-btn' + (view === id ? ' active' : '')}
      aria-label={label}
      {...tipProps(label)}
      onClick={() => onView(id)}
    >
      <Icon name={icon} />
      {accent && <AppDot accent={accent} className="rail-dot" />}
    </button>
  );

  return (
    <aside id="drawer" className={open ? 'open' : ''}>
      {/* Narrow icon rail (claude.ai style) — panel toggle on top, then new chat,
          the Assistant, and the user's connected apps. Vitals / Operations /
          Data / Setup live in the settings flyout at the foot — as they do in
          the expanded panel, from the same list. */}
      <div className="side-rail">
        <button
          className="rail-btn rail-toggle"
          aria-label={open ? 'Close sidebar' : 'Open sidebar'}
          aria-expanded={open}
          {...tipProps(open ? 'Close sidebar' : 'Open sidebar')}
          onClick={() => { onToggle(); setTip(null); }}
        >
          <Icon name="sidebar" />
        </button>
        <button
          className="rail-btn rail-new"
          aria-label="New chat"
          {...tipProps('New chat')}
          onClick={onNewChat}
        >
          <Icon name="plus" />
        </button>
        <div className="rail-tabs">
          {railBtn('chat', `${brand} — Assistant`, 'bot')}
          {/* App tabs — derived from the connector registry, below the chat icon. */}
          {userApps.map((a) => railBtn(a.id, a.label, appIcon(a), appAccent(a)))}
          {/* …and the way to get one more. Collapsed is the sidebar's default on
              a phone, so the affordance has to survive the collapse or it only
              exists for desktop users who happen to have the panel open. */}
          <button
            className="rail-btn rail-connect"
            aria-label="Connect your app"
            {...tipProps('Connect your app')}
            onClick={() => { setConnectOpen(true); setTip(null); }}
          >
            <Icon name="plug" />
          </button>
        </div>
        {tip && createPortal(
          <div className="rail-tip" style={{ top: tip.top }}>{tip.label}</div>,
          document.body,
        )}
        <div className="rail-spacer" />
        <div className="rail-foot">
          <RailFlyout items={NAV_SYSTEM} view={view} onView={onView} drawerOpen={open} />
        </div>
      </div>

      {/* Expanded panel (claude.ai layout): wordmark head with search + collapse,
          the circled "+ New chat", primary destinations, then the sectioned
          groups — connected Apps and the Recents chat history. */}
      <div className="side-panel">
        <div className="panel-head">
          <BrandWord name={brand} />
          <div className="panel-head-actions">
            <button type="button"
              className={'ibtn' + (searchOpen ? ' on' : '')}
              title="Search chats"
              aria-label="Search chats"
              aria-expanded={searchOpen}
              onClick={() => { setSearchOpen((o) => !o); if (searchOpen) setQuery(''); }}
            >
              <Icon name="search" />
            </button>
            <button type="button"
              className="ibtn"
              title="Close sidebar"
              aria-label="Close sidebar"
              onClick={onToggle}
            >
              <Icon name="sidebar" />
            </button>
          </div>
        </div>

        {searchOpen && (
          <input
            className="panel-search"
            type="search"
            placeholder="Search chats…"
            value={query}
            autoFocus
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Escape') { setSearchOpen(false); setQuery(''); } }}
          />
        )}

        <button type="button" className="nav-item nav-new" aria-label="New chat" onClick={onNewChat}>
          <span className="nav-new-ic"><Icon name="plus" /></span>
          <span>New chat</span>
        </button>

        {/* Chats, alone. The dashboards-and-settings destinations are not here —
            they are behind the flyout pinned at the foot of this panel. */}
        <nav className="nav-list" aria-label="Primary">
          {NAV_INLINE.map((it) => (
            <button type="button"
              key={it.id}
              className={'nav-item' + (view === it.id ? ' active' : '')}
              onClick={() => onView(it.id)}
            >
              <Icon name={it.icon} className="nav-ic" />
              <span>{it.label}</span>
            </button>
          ))}
        </nav>

        {/* Apps — shown even with nothing connected. An empty section is not
            clutter here: it is the only place the sidebar says that connecting
            your own app is a thing Ava does, and it carries the button that
            does it. The connect row sits OUTSIDE nav[aria-label="Apps"] so that
            list stays exactly the connected apps — it is read as such by
            appIcon()/appAccent() checks, and a control mixed in would be one
            more "app" with no identity of its own. */}
        <div className="draw-sub">Apps</div>
        {userApps.length > 0 && (
          <nav className="nav-list" aria-label="Apps">
            {userApps.map((a) => (
              <button type="button"
                key={a.id}
                className={'nav-item' + (view === a.id ? ' active' : '')}
                onClick={() => onView(a.id)}
              >
                <Icon name={appIcon(a)} className="nav-ic" />
                <span>{a.label}</span>
                <AppDot accent={appAccent(a)} className="nav-app-dot" />
              </button>
            ))}
          </nav>
        )}
        <div className="nav-list">
          <button type="button"
            className="nav-item nav-connect"
            onClick={() => setConnectOpen(true)}
          >
            <Icon name="plug" className="nav-ic" />
            <span>Connect your app</span>
          </button>
        </div>

        <div className="draw-sub">Recents</div>
        <div id="chatList">
          {visibleChats.length === 0 ? (
            <div className="draw-empty">{q ? 'No matching chats' : 'No conversations yet'}</div>
          ) : (
            visibleChats.map((it) => (
              // A real <button>, not a <div onClick>. The row was a div because a
              // button cannot nest a button — so the fix is to UNNEST rather than
              // bolt tabIndex + onKeyDown onto a div. Before this, `tabIndex`
              // appeared zero times in the whole frontend: the only
              // keyboard-reachable control per row was the delete button, which
              // is opacity:0 until hover. So a keyboard user could reach exactly
              // one invisible control per conversation, and it deleted it.
              // WCAG 2.1.1 Level A, with no alternate route — DataView's chat
              // rows offer export and delete, but never open.
              <div
                key={it.id}
                className={'chatitem-wrap' + (it.id === currentChatId ? ' active' : '')}
              >
                <button
                  type="button"
                  className="chatitem"
                  aria-current={it.id === currentChatId ? 'page' : undefined}
                  onClick={() => onOpenChat(it.id)}
                >
                  <div className="ci-main">
                    <div className="ci-title">{it.title || 'New chat'}</div>
                  </div>
                </button>
                <button
                  type="button"
                  className="del"
                  title="Delete chat"
                  aria-label={`Delete chat: ${it.title || 'New chat'}`}
                  onClick={() => {
                    // Reachable by keyboard now, so a stray Enter must not be
                    // silent data loss — every other destructive action in the
                    // app confirms (DataView, ConnectorsPanel, AgentPanel).
                    if (window.confirm(`Delete "${it.title || 'New chat'}"? This cannot be undone.`)) {
                      onDeleteChat(it.id);
                    }
                  }}
                >
                  <Icon name="trash" />
                </button>
              </div>
            ))
          )}
        </div>

        {/* Pinned foot. The four system destinations moved off the nav list and
            behind this row: the sidebar's job is the thing you are doing — your
            chats, your apps, your history — and four dashboards standing
            permanently above them made the panel a control surface with a chat
            list attached. Mirrors the collapsed rail's foot flyout: same items,
            same menu, one list (NAV_SYSTEM), so the two forms cannot drift. */}
        <div className="panel-foot">
          <PanelFlyout items={NAV_SYSTEM} view={view} onView={onView} drawerOpen={open} />
        </div>
      </div>

      {/* Portalled to <body> by the dialog itself — the drawer clips overflow
          and is under the app shell's stacking context. It fires
          `ava:apps-changed` on a successful connect, which is what redraws the
          rail (App.tsx re-fetches /api/apps), so a new app appears behind the
          dialog while it is still open. */}
      {connectOpen && <ConnectAppDialog onClose={() => setConnectOpen(false)} />}
    </aside>
  );
}
