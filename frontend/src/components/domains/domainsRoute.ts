/** The Domains view's slice of the address bar.
 *
 * App.tsx owns hash segment 0 and nothing else; this owns the rest. Pure, with
 * no React and no window access, so the arithmetic is testable on its own —
 * the same split `hub/hubRoute.ts` documents.
 */
export interface DomainsFocus {
  realm: string | null;
  domain: string | null;
}

const EMPTY: DomainsFocus = { realm: null, domain: null };

function decode(seg: string): string {
  try {
    return decodeURIComponent(seg);
  } catch {
    // A lone '%' in the address bar throws. A malformed hash is a mistyped
    // bookmark, not a crash: fall back to the raw segment.
    return seg;
  }
}

/** `#domains/<realm>/<domain>` -> both; anything else -> neither. */
export function focusFromHash(hash: string): DomainsFocus {
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  if (parts.length < 3 || parts[0] !== 'domains') return EMPTY;
  return { realm: decode(parts[1]), domain: decode(parts[2]) };
}

export function hashForCell(realm: string, domain: string): string {
  return `#domains/${encodeURIComponent(realm)}/${encodeURIComponent(domain)}`;
}
