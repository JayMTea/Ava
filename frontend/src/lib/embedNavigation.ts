/** Only an app-relative route from the expected frame can update the shell address. */
export function frameNavigation(event: MessageEvent, frame: Window, origin: string, id: string): string | null {
  if (event.source !== frame || event.origin !== origin) return null;
  const data = event.data;
  if (!data || data.type !== 'ava:navigation' || data.cid !== id || typeof data.path !== 'string') return null;
  if (!data.path.startsWith('/') || data.path.startsWith('//') || data.path.includes('\\') || data.path.length > 8192) return null;
  const prefix = `/apps/${encodeURIComponent(id)}/`;
  const url = new URL(prefix + data.path.slice(1), origin);
  if (url.origin !== origin || !url.pathname.startsWith(prefix)) return null;
  const path = '/' + url.pathname.slice(prefix.length);
  if (/^\/(login|logout|auth|\.ava)(\/|$)/i.test(path)) return null;
  for (const key of [...url.searchParams.keys()]) {
    if (/^(t|theme|embedded|v)$|token|password|secret|credential|authorization/i.test(key)) url.searchParams.delete(key);
  }
  return path + url.search + url.hash;
}
