import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
  RefObject,
} from 'react';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { placeAt } from '../components/flyoutPlace';
import { Icon } from './icons';
import type { MenuAction } from './RowMenu';

// A right-click menu. The sidebar flyout's chrome (.rail-menu, shared with
// RowMenu) anchored at the POINTER instead of at a trigger, and living for one
// interaction: pick an item, press Escape, click or scroll anywhere else, and
// it is gone. Portalled to <body> like its siblings, so a clipping ancestor â€”
// the drawer is overflow:hidden â€” can never cut it off.
//
// THE KEYBOARD OPENS IT TOO. Shift+F10 and the Menu key fire `contextmenu` on
// the focused element with no pointer position, and a menu that only exists
// under a mouse is a menu a keyboard user cannot reach (WCAG 2.1.1) â€”
// contextPoint() puts that case just inside the element instead. Once open it
// is a WAI-ARIA menu: focus moves in, arrows rove, Escape hands focus back to
// the element it came from.

export interface ContextMenuState {
  x: number;
  y: number;
  /** What was right-clicked. Focus returns here on Escape or a pick, so a
   *  keyboard user lands back where they were rather than on <body>. */
  from: HTMLElement | null;
  actions: MenuAction[];
  /** The menu's accessible name: what it is a menu FOR. */
  label: string;
}

/** Where a `contextmenu` event wants its menu: at the pointer, or â€” when the
 *  keyboard fired it, which reports (0, 0) â€” just inside the element's own
 *  bottom-left corner, where the eye already is. */
export function contextPoint(e: ReactMouseEvent<HTMLElement>): { x: number; y: number } {
  if (e.clientX || e.clientY) return { x: e.clientX, y: e.clientY };
  const r = e.currentTarget.getBoundingClientRect();
  return { x: r.left + Math.min(12, r.width / 2), y: r.bottom - 2 };
}

/** The list itself â€” portal-free, so a static render can check its shape. */
export function ContextMenuList({
  label, actions, cursor, itemRefs, menuRef, style, onPick, onKeyDown,
}: {
  label: string;
  actions: MenuAction[];
  cursor: number;
  itemRefs: RefObject<(HTMLButtonElement | null)[]>;
  menuRef: RefObject<HTMLDivElement | null>;
  style: CSSProperties;
  onPick: (a: MenuAction) => void;
  onKeyDown: (e: ReactKeyboardEvent) => void;
}) {
  return (
    <div
      ref={menuRef}
      className="rail-menu ctx-menu"
      role="menu"
      aria-orientation="vertical"
      aria-label={label}
      style={style}
      onKeyDown={onKeyDown}
    >
      {actions.map((a, i) => (
        <button type="button"
          key={a.label}
          // Braces, not a concise body: React reads a ref callback's RETURN
          // value as a cleanup function.
          ref={(el) => { itemRefs.current[i] = el; }}
          role="menuitem"
          // Roving tabindex: exactly one item in the tab order, arrows do the rest.
          tabIndex={i === cursor ? 0 : -1}
          className={'rail-menu-item' + (a.danger ? ' danger' : '')}
          disabled={a.disabled}
          onClick={() => onPick(a)}
        >
          <Icon name={a.icon} />
          <span>{a.label}</span>
        </button>
      ))}
    </div>
  );
}

export function ContextMenu({ menu, onClose }: { menu: ContextMenuState; onClose: () => void }) {
  const menuRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [pos, setPos] = useState({ left: menu.x, top: menu.y });
  const [cursor, setCursor] = useState(0);
  const { from, actions } = menu;

  // Focus goes back to the tile when the KEYBOARD or a PICK closes the menu,
  // and stays where the pointer put it when an outside click does â€” moving it
  // then would yank the caret out of whatever the click landed in.
  const dismiss = useCallback((restoreFocus: boolean) => {
    if (restoreFocus && from?.isConnected) from.focus({ preventScroll: true });
    onClose();
  }, [from, onClose]);

  // Measured, placed and focused before paint. The natural size is only known
  // once the portal exists, so it renders at the point first and this moves it
  // in the same frame â€” no flash at the wrong place. Re-run per menu: a
  // right-click on a second tile while one is open replaces the state.
  useLayoutEffect(() => {
    const r = menuRef.current?.getBoundingClientRect();
    if (r) {
      setPos(placeAt(
        { x: menu.x, y: menu.y },
        { width: r.width, height: r.height },
        { width: window.innerWidth, height: window.innerHeight },
      ));
    }
    setCursor(0);
    itemRefs.current[0]?.focus({ preventScroll: true });
  }, [menu]);
  // One place moves focus after that: the cursor changes, this puts focus on it.
  useLayoutEffect(() => {
    itemRefs.current[cursor]?.focus({ preventScroll: true });
  }, [cursor]);

  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (menuRef.current?.contains(e.target as Node)) return;
      dismiss(false);
    };
    // Capture phase, like the flyout's: React listens at the root container,
    // below document, so a bubble-phase handler here would run after the
    // panel's search field had already seen the Escape.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      dismiss(true);
    };
    // Scroll and resize move the tile out from under a fixed menu; a window
    // losing focus is the new window arriving, or the user leaving.
    const away = () => dismiss(false);
    document.addEventListener('pointerdown', onDown, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('scroll', away, true);
    window.addEventListener('resize', away);
    window.addEventListener('blur', away);
    return () => {
      document.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('scroll', away, true);
      window.removeEventListener('resize', away);
      window.removeEventListener('blur', away);
    };
  }, [dismiss]);

  const onKeyDown = (e: ReactKeyboardEvent) => {
    const n = actions.length;
    const found = itemRefs.current.findIndex((el) => el === document.activeElement);
    const at = found < 0 ? 0 : found;
    const to = (i: number) => setCursor(((i % n) + n) % n);
    switch (e.key) {
      case 'ArrowDown': e.preventDefault(); to(at + 1); break;
      case 'ArrowUp': e.preventDefault(); to(at - 1); break;
      case 'Home': e.preventDefault(); to(0); break;
      case 'End': e.preventDefault(); to(n - 1); break;
      // Not prevented: focus goes back to the tile synchronously and the
      // browser's own Tab then runs from there, exactly as if the menu had
      // never opened. Escape is the document listener above.
      case 'Tab': dismiss(true); break;
      default: break;
    }
  };

  // Close FIRST, act second: the action may open a window, and the menu must
  // not still be standing when focus comes back to this one.
  const onPick = (a: MenuAction) => { dismiss(true); a.onClick(); };

  return createPortal(
    <ContextMenuList
      label={menu.label}
      actions={actions}
      cursor={cursor}
      itemRefs={itemRefs}
      menuRef={menuRef}
      style={pos}
      onPick={onPick}
      onKeyDown={onKeyDown}
    />,
    document.body,
  );
}
