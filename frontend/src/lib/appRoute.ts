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
