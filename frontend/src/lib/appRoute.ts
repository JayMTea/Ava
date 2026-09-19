export type AppRoute = { view: string; path: string };

// The shell fragment may carry an app-relative destination, including its query.
// AppFrame validates it again against the bridge-issued app origin and prefix.
export function appRouteFromHash(hash: string): AppRoute | null {
  const match = /^#\/?([\w-]+)(\/.*)?$/.exec(hash);
  if (!match) return null;
  const view = match[1];
  const path = match[2] || '/';
  const prefix = `/apps/${view}/`;
  const target = new URL(prefix + path.slice(1), 'https://shell.invalid');
  if (!target.pathname.startsWith(prefix)) return null;
  return { view, path };
}

// Does the fragment name a destination INSIDE the app, or only the tile?
//
// `#infra` and `#infra/` both resolve to path '/', and the difference decides
// what the shell may forget: the first is "open Home Lab", which should land
// where it was left, and the second is "open Home Lab at its home", which says
// so. Only an explicit destination overwrites the remembered path.
export function appRouteIsExplicit(hash: string): boolean {
  return appRouteFromHash(hash) !== null && /^#\/?[\w-]+\/.*$/.test(hash);
}

/** Where each hosted app was last seen. Keys are app ids, values app-relative paths. */
export type AppPaths = Record<string, string>;

/**
 * The remembered paths a stored blob is allowed to contribute.
 *
 * Storage is same-origin, so this is not a trust boundary — it is a shape
 * check. A value that survived a downgrade, a hand edit or a half-written
 * write reaches `appFrameDestination`, which THROWS on a path it cannot
 * resolve, and that rejection lands in the mint's catch and paints the frame
 * dead. A frame that will not open because of a stale string in localStorage
 * is not a state anyone can diagnose from the UI, so the junk stops here.
 */
export function appPathsFrom(raw: unknown, known?: string[]): AppPaths {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  const paths: AppPaths = {};
  for (const [id, path] of Object.entries(raw as Record<string, unknown>)) {
    if (!/^[\w-]+$/.test(id) || (known && !known.includes(id))) continue;
    if (typeof path !== 'string' || path.length > 8192) continue;
    if (!path.startsWith('/') || path.startsWith('//') || path.includes('\\')) continue;
    if (appRouteFromHash(`#${id}${path === '/' ? '' : path}`) === null) continue;
    paths[id] = path;
  }
  return paths;
}
