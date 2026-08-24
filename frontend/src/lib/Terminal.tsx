// PARKED, not dead. Nothing imports this today, so xterm is not bundled.
// TerminalPanel gates instead of rendering, because how a live terminal
// delivers OUTPUT has not been captured (see its header). This renderer is
// correct and stays here for the slice that finishes that wiring; the moment
// it is imported again, xterm returns to the bundle.

import { FitAddon } from '@xterm/addon-fit';
import { Terminal as XTerm } from '@xterm/xterm';
import { useEffect, useRef } from 'react';
import '@xterm/xterm/css/xterm.css';

// A real terminal, drawn by Ava.
//
// WHY NATIVE RATHER THAN AN EMBED
// -------------------------------
// `terminal.open` / `terminal.input` / `terminal.resize` are ordinary gateway
// RPC methods, so a native panel rides the same socket and the same
// `/api/gateway/rpc` route as every other panel — and inherits Ava's session
// auth, Ava's theme, and the audit ledger for free. An embed would have needed
// the full websocket reverse proxy, would carry the upstream's own chrome, and
// nothing it did would be attributable.
//
// The cost is honest: xterm takes the frontend from three runtime dependencies
// to five. It buys cursor addressing, colour and anything interactive — which a
// line-oriented console cannot do, and which is most of what a terminal is for.
//
// Lives under `src/lib/` so `vite.config.ts`'s manual chunk for
// `/src/components/agent/` cannot capture it: manualChunks WINS over nested
// lazy(), so anything swept into that chunk can never be split out again.

export interface TerminalProps {
  /** Called with whatever the user types. */
  onInput(data: string): void;
  /** Called when the viewport changes size, so the far end can reflow. */
  onResize?(cols: number, rows: number): void;
  /** Imperative sink for output — the caller pushes bytes in. */
  sinkRef?: { current: ((data: string) => void) | null };
}

/**
 * Read the app's own tokens, so a re-branded install reaches inside the term.
 *
 * xterm needs concrete colour STRINGS — it paints to a canvas and cannot resolve
 * `var(--accent)` — so the tokens are resolved here rather than referenced.
 *
 * The fallbacks are deliberately NEUTRAL greys and not Ava's shipped palette.
 * `tests/test_brand_tokens.py` exists because a literal accent anywhere outside
 * `tokens.css` is a colour a re-brand cannot reach, and a fallback is not an
 * exception to that: if the token is genuinely missing, something is very wrong
 * and a neutral is more honest than confidently painting a brand colour the
 * install may have replaced.
 */
function themeFromTokens(): Record<string, string> {
  const cs = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) =>
    (cs.getPropertyValue(name) || '').trim() || fallback;
  return {
    background: v('--panel2', '#222'),
    foreground: v('--txt', '#eee'),
    cursor: v('--accent', '#888'),
    selectionBackground: v('--accent-soft', 'rgba(128,128,128,.35)'),
    black: v('--panel', '#222'),
    red: v('--err', '#c33'),
    green: v('--ok', '#3a5'),
    yellow: v('--warn', '#ca3'),
    blue: v('--accent', '#888'),
    cyan: v('--info', '#59a'),
    white: v('--txt', '#eee'),
    brightBlack: v('--muted', '#999'),
  };
}

export function Terminal({ onInput, onResize, sinkRef }: TerminalProps) {
  const hostRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const term = new XTerm({
      fontFamily: getComputedStyle(document.documentElement)
        .getPropertyValue('--font-mono').trim() || 'monospace',
      fontSize: 12,
      convertEol: true,
      cursorBlink: true,
      theme: themeFromTokens(),
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    fit.fit();

    const disp = term.onData(onInput);
    if (sinkRef) sinkRef.current = (d: string) => term.write(d);

    // ResizeObserver rather than a window listener: the panel is resizable
    // independently of the window (the console's third column drags), and a
    // window listener would miss every one of those.
    const ro = new ResizeObserver(() => {
      try {
        fit.fit();
        onResize?.(term.cols, term.rows);
      } catch {
        /* a detached or zero-size host — nothing to fit */
      }
    });
    ro.observe(host);
    onResize?.(term.cols, term.rows);

    // Re-theme in place on a theme switch. Recreating the terminal would work
    // and would also throw away the scrollback and kill the PTY's attachment,
    // which is the same reason the Agent view is kept mounted at all.
    const mo = new MutationObserver(() => { term.options.theme = themeFromTokens(); });
    mo.observe(document.documentElement, {
      attributes: true, attributeFilter: ['data-theme', 'data-brand'],
    });

    return () => {
      mo.disconnect();
      ro.disconnect();
      disp.dispose();
      if (sinkRef) sinkRef.current = null;
      term.dispose();
    };
    // Mount once. `onInput`/`onResize` are read through the closure captured
    // here on purpose: re-running this effect would tear down a live PTY view.
  }, []);

  return <div className="term-host" ref={hostRef} />;
}

export default Terminal;
