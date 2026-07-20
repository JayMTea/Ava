import type { AppEntry } from './types';

// Connected-app identity accents (see CLAUDE.md → "Other conventions").
// Every UI element that represents a connected app — sidebar entries, chat
// tool chips, artifact/preview cards, app headers — carries the app's accent
// so it visibly belongs to the app, not to Ava. The color is the connector's
// `ui.color` (manifest fact, passed through /api/apps) when declared, else a
// stable pick from the --app-accent-* token palette hashed from the app id,
// so it never changes between sessions and needs zero config.

const PALETTE_SIZE = 8;

/** The palette slot indexes the Hub's appearance picker offers — the same pool
 *  appAccent() auto-picks from, so a chosen accent is always one Ava's theme
 *  already defines (and stays correct in both light and dark). */
export const ACCENT_SLOTS = Array.from({ length: PALETTE_SIZE }, (_, i) => i);

function hashId(id: string): number {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return h;
}

/** The CSS color for an app's identity accent: the manifest `ui.color` when
 *  the connector declares one, else a stable palette pick hashed from the id.
 *  A bare id resolves through the registry so overrides still apply. */
export function appAccent(app: Pick<AppEntry, 'id' | 'color'> | string): string {
  const entry = typeof app === 'string' ? knownApps.find((a) => a.id === app) : app;
  if (entry?.color) return entry.color;
  const id = typeof app === 'string' ? app : app.id;
  return `var(--app-accent-${hashId(id) % PALETTE_SIZE})`;
}

// Curated, app-flavored glyphs (a subset of lib/icons that reads as "an app",
// not Ava's own chrome like mic/sidebar/sliders). Shared by the auto-pick below
// AND the Hub appearance picker, so the default set and the choosable set are
// always identical — add a glyph here and it appears in both.
export const APP_ICONS = [
  'grid', 'panel', 'cloud', 'db', 'chart', 'gauge', 'activity', 'sparkles',
  'calendar', 'megaphone', 'graduation', 'pin', 'file', 'image', 'user',
] as const;

/** The glyph name for an app's rail/nav icon: the manifest `ui.icon` when the
 *  connector declares one, else a STABLE pick from APP_ICONS hashed from the id
 *  — so apps that don't declare an icon still differ from each other with zero
 *  config, exactly the way appAccent() varies the color. Never changes between
 *  sessions. A bare id resolves through the registry so overrides still apply. */
export function appIcon(app: Pick<AppEntry, 'id' | 'icon'> | string): string {
  const entry = typeof app === 'string' ? knownApps.find((a) => a.id === app) : app;
  if (entry?.icon) return entry.icon;
  const id = typeof app === 'string' ? app : app.id;
  return APP_ICONS[hashId(id) % APP_ICONS.length];
}

// ---- App registry (for attributing tools / URLs back to an app) ------------
// App.tsx registers the /api/apps result here on load, so any component can
// resolve "which app does this belong to?" without prop-threading the list.
// Core-section entries are Ava's own surfaces and never attributed.

let knownApps: AppEntry[] = [];

export function registerApps(apps: AppEntry[]): void {
  knownApps = apps.filter((a) => a.section !== 'core');
}

/** The app a sandbox tool belongs to, from the generated-tool naming
 *  convention `<connectorId>_<action>` (also tolerates mcp `srv__tool`). */
export function appForTool(tool: string): AppEntry | undefined {
  const t = String(tool).replace(/^.*__/, '');
  return knownApps.find((a) => t === a.id || t.startsWith(a.id + '_'));
}

/** The app behind a same-origin proxied URL (/apps/<id>/…) — e.g. a
 *  chat_pickup preview card's image. */
export function appForUrl(url: string): AppEntry | undefined {
  const m = /^\/apps\/([^/]+)\//.exec(String(url));
  return m ? knownApps.find((a) => a.id === decodeURIComponent(m[1])) : undefined;
}

/** The registered app for a connector id (undefined if it isn't a rail app). */
export function appById(id: string | null | undefined): AppEntry | undefined {
  return id ? knownApps.find((a) => a.id === id) : undefined;
}

/** The small identity dot. Decorative — always pair it with the app's name
 *  (visible text nearby, or at least a `title` tooltip). */
export function AppDot({ accent, className, title }: {
  accent: string; className?: string; title?: string;
}) {
  return (
    <span
      className={'app-dot' + (className ? ' ' + className : '')}
      style={{ background: accent }}
      title={title}
      aria-hidden="true"
    />
  );
}

/** Distinct connected apps behind a tool list — for marking a skill (or any
 *  capability bundle) with the app(s) it drives. */
export function appsForTools(tools: string[]): AppEntry[] {
  const seen = new Map<string, AppEntry>();
  for (const t of tools) {
    const a = appForTool(t);
    if (a) seen.set(a.id, a);
  }
  return [...seen.values()];
}
