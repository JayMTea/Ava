// Theme (light / dark) — one source of truth for the whole app.
//
// The actual colors live in styles/tokens.css: dark is the :root default and
// `:root[data-theme="light"]` overrides it. This module only decides WHICH
// theme is active and stamps `data-theme` on <html>, so any page — current or
// future — inherits it automatically with no per-component wiring.
//
// Resolution order: explicit user choice (localStorage) → OS preference → dark.
// The initial stamp happens in index.html before first paint to avoid a flash;
// this module keeps it in sync at runtime and lets React subscribe.

export type Theme = 'light' | 'dark';

const KEY = 'ava.theme';

function systemPrefersLight(): boolean {
  return typeof window !== 'undefined'
    && window.matchMedia?.('(prefers-color-scheme: light)').matches;
}

export function getTheme(): Theme {
  if (typeof document !== 'undefined') {
    const attr = document.documentElement.getAttribute('data-theme');
    if (attr === 'light' || attr === 'dark') return attr;
  }
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === 'light' || saved === 'dark') return saved;
  } catch { /* storage unavailable */ }
  return systemPrefersLight() ? 'light' : 'dark';
}

export function applyTheme(theme: Theme): void {
  if (typeof document === 'undefined') return;
  document.documentElement.setAttribute('data-theme', theme);
}

export function setTheme(theme: Theme): void {
  applyTheme(theme);
  try { localStorage.setItem(KEY, theme); } catch { /* storage unavailable */ }
  window.dispatchEvent(new CustomEvent<Theme>('ava:theme', { detail: theme }));
}

export function toggleTheme(): Theme {
  const next: Theme = getTheme() === 'dark' ? 'light' : 'dark';
  setTheme(next);
  return next;
}
