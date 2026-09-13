import type { MenuAction } from './RowMenu';

// Opening a destination somewhere other than this tab â€” the verbs behind the
// sidebar's right-click menu (Drawer.tsx, `openElsewhere`).
//
// Every sidebar tile that stands for a view is an <a href="#view"> (Drawer.tsx,
// "A destination is a LINK"), so the browser's own gestures â€” middle-click,
// Ctrl/Cmd-click, dragging one to the tab strip â€” have always worked on the
// rail and the panel rows. What was missing was a menu that SAID so, and the
// destinations inside the "Settings & dashboards" flyout are <button>s in a
// menu, which none of those gestures reach. These actions give every tile the
// same three verbs from one place.
//
// WHAT OPENS IS THE SHELL, NEVER THE BARE APP. An iframe app's own address
// (/apps/<id>/) is deliberately not offered: the bridge bounces a top-level
// visit to it back into the shell (phone_bridge.app_ui_proxy), because the bare
// app has no sidebar, no chat and no way back. A tile opened in a new window is
// Ava, at that tile â€” the app fills the window with the shell around it.

export interface ShellLocation { origin: string; pathname: string }

/** A view's address for another tab or window: this shell's document with the
 *  view as hash segment 0 â€” the one segment App.tsx owns. The current search
 *  is dropped on purpose: `?artifact=` opens a chat artifact and belongs to
 *  the tab that had it, and the new window was asked for a tile, not a chat. */
export function shellUrl(view: string, loc: ShellLocation = window.location): string {
  return `${loc.origin}${loc.pathname}#${view}`;
}

/** The subset of `Screen` this needs, so a test can pass a plain object.
 *  availLeft/availTop are non-standard but universal on desktop browsers, and
 *  are what keep the new window on the monitor the shell is on. */
export interface ScreenArea {
  availWidth: number;
  availHeight: number;
  availLeft?: number;
  availTop?: number;
}

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

/** window.open features for a new window. A page cannot ask for a tab or a
 *  full browser window â€” that choice is the browser's â€” but a URL opened with
 *  a size IS the popup form, which is a real separate window (and, from an
 *  installed PWA, a second app window). Most of the screen and centred on it:
 *  never narrower than the sidebar plus a usable chat column, never wider
 *  than the shell can use. `noopener`: the new window is a second Ava that
 *  shares nothing with this one and needs no handle back to it. */
export function popupFeatures(s: ScreenArea): string {
  const width = clamp(Math.round(s.availWidth * 0.8), Math.min(760, s.availWidth), 1440);
  const height = clamp(Math.round(s.availHeight * 0.85), Math.min(560, s.availHeight), 1000);
  const left = (s.availLeft ?? 0) + Math.round((s.availWidth - width) / 2);
  const top = (s.availTop ?? 0) + Math.round((s.availHeight - height) / 2);
  return `popup=yes,width=${width},height=${height},left=${left},top=${top},noopener`;
}

export type OpenWhere = 'tab' | 'window';

/** Open `view` in another tab or window of this browser. Synchronous on
 *  purpose: window.open is only allowed inside the click that asked for it. */
export function openShell(view: string, where: OpenWhere): void {
  const url = shellUrl(view);
  if (where === 'window') {
    window.open(url, '_blank', popupFeatures(window.screen));
    return;
  }
  // No features at all, so the browser treats it as an ordinary new tab. The
  // opener is cut afterwards for the same reason the window form says noopener.
  const w = window.open(url, '_blank');
  if (w) w.opener = null;
}

/** The verbs every tile's right-click menu offers. "Copy link" only where the
 *  clipboard is reachable: navigator.clipboard is undefined on a plain-http
 *  LAN install (not a secure context), and a menu item that silently does
 *  nothing is worse than one that is not there. */
export function openElsewhereActions(view: string): MenuAction[] {
  const out: MenuAction[] = [
    { label: 'Open in new tab', icon: 'external', onClick: () => openShell(view, 'tab') },
    { label: 'Open in new window', icon: 'window', onClick: () => openShell(view, 'window') },
  ];
  const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard;
  if (clipboard?.writeText) {
    out.push({
      label: 'Copy link',
      icon: 'copy',
      onClick: () => { clipboard.writeText(shellUrl(view)).catch(() => { /* denied: nothing to show */ }); },
    });
  }
  return out;
}
