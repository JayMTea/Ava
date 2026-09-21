/**
 * What an app may say about where it is, and what of it the shell will keep.
 *
 * A reported route is mirrored into Ava's own address bar and remembered in
 * localStorage, so anything left in it outlives the app's page: it lands in
 * browser history, in a session restore, in a screenshot of the address bar.
 * That is why credentials are stripped here rather than trusted to the app —
 * and why the fragment is stripped as well as the query. A sign-in that answers
 * in the fragment is the common shape (OIDC and Supabase implicit flows land on
 * `#access_token=…`), and it arrives at exactly the moment an app is reporting
 * its route. Every iframe app reports now, including ones the owner does not
 * control, so this is the one place that has to hold.
 */
const CREDENTIAL = /^(t|theme|embedded|v)$|token|password|secret|credential|authorization/i;

/**
 * The same pairs minus the credential-looking ones, byte-for-byte for the rest.
 *
 * Deliberately not `URLSearchParams`: writing through it re-encodes every
 * survivor (`%20` becomes `+`, `:` becomes `%3A`), and this string is what the
 * frame is REOPENED at. An app that reads its own query with anything other
 * than `URLSearchParams` would be handed a value it never wrote.
 */
function withoutCredentials(raw: string): string {
  const body = raw.slice(1);
  if (!body) return '';
  const kept = body.split('&').filter((pair) => {
    const key = pair.split('=')[0];
    let name = key;
    try {
      name = decodeURIComponent(key.replace(/\+/g, ' '));
    } catch { /* undecodable: judge it by the spelling it arrived in */ }
    return !CREDENTIAL.test(name);
  });
  return kept.length ? raw[0] + kept.join('&') : '';
}

/** `#access_token=…&state=…` is a parameter set; `#/settings` or `#results` is a route. */
function fragmentCarriesParameters(hash: string): boolean {
  return /^#[^=&]+=[^&]*(&[^&]*)*$/.test(hash);
}

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
  const hash = fragmentCarriesParameters(url.hash) ? withoutCredentials(url.hash) : url.hash;
  return path + withoutCredentials(url.search) + hash;
}
