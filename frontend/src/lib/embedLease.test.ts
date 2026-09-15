/** The re-mint arithmetic behind AppFrame, without a browser.
 *
 * What is NOT covered here is the DOM half — the visibility listener, the message
 * listener and the keepalive timer live in AppFrame.tsx and are exercised in a real
 * browser, not in this file. What IS pinned is every decision they defer to.
 */
import { describe, expect, it } from 'vitest';
import {
  DEFAULT_COOKIE_TTL_S,
  DEFAULT_TOKEN_TTL_S,
  expiredFramePath,
  keepaliveIntervalMs,
  keepaliveUrl,
  leaseExpired,
  leaseFrom,
} from './embedLease';

const ORIGIN = 'https://apps.ava.test:10443';
const SRC = `${ORIGIN}/apps/app-a/?theme=dark&embedded=1&t=1.abc`;
const NOW = 1_700_000_000_000;

describe('leaseFrom', () => {
  it('keeps the lifetimes the bridge stated, in milliseconds', () => {
    const lease = leaseFrom({ url: SRC, token_ttl_s: 60, cookie_ttl_s: 3600 }, SRC, ORIGIN, NOW);
    expect(lease).toEqual({ src: SRC, origin: ORIGIN, mintedAt: NOW, tokenTtlMs: 60_000, cookieTtlMs: 3_600_000 });
  });

  it('falls back to the documented defaults for a bridge that predates them', () => {
    const lease = leaseFrom({ url: SRC }, SRC, ORIGIN, NOW);
    expect(lease.tokenTtlMs).toBe(DEFAULT_TOKEN_TTL_S * 1000);
    expect(lease.cookieTtlMs).toBe(DEFAULT_COOKIE_TTL_S * 1000);
  });

  it('ignores a nonsensical lifetime rather than minting forever or never', () => {
    const lease = leaseFrom({ url: SRC, token_ttl_s: 0, cookie_ttl_s: Number.NaN }, SRC, ORIGIN, NOW);
    expect(lease.tokenTtlMs).toBe(DEFAULT_TOKEN_TTL_S * 1000);
    expect(lease.cookieTtlMs).toBe(DEFAULT_COOKIE_TTL_S * 1000);
  });
});

describe('leaseExpired', () => {
  const lease = leaseFrom({ url: SRC, token_ttl_s: 300, cookie_ttl_s: 43_200 }, SRC, ORIGIN, NOW);

  it('judges a frame that never loaded by the URL token', () => {
    expect(leaseExpired(lease, false, NOW + 200_000)).toBe(false);
    expect(leaseExpired(lease, false, NOW + 300_000)).toBe(true);
  });

  it('judges a loaded frame by the cookie, which is what the frame actually holds', () => {
    expect(leaseExpired(lease, true, NOW + 300_000)).toBe(false);
    expect(leaseExpired(lease, true, NOW + 6 * 3_600_000)).toBe(false);
    expect(leaseExpired(lease, true, NOW + 12 * 3_600_000)).toBe(true);
  });

  it('errs towards minting again just before the deadline, never just after it', () => {
    // 10% early: a resume that races the bridge's clock lands on a fresh token,
    // not on the reconnect page.
    expect(leaseExpired(lease, false, NOW + 270_000)).toBe(true);
    expect(leaseExpired(lease, false, NOW + 269_000)).toBe(false);
  });
});

describe('keepalive', () => {
  it('touches the apps origin a quarter of the cookie life apart, never faster than a minute', () => {
    expect(keepaliveIntervalMs(leaseFrom({ url: SRC, cookie_ttl_s: 43_200 }, SRC, ORIGIN, NOW))).toBe(3 * 3_600_000);
    expect(keepaliveIntervalMs(leaseFrom({ url: SRC, cookie_ttl_s: 120 }, SRC, ORIGIN, NOW))).toBe(60_000);
  });

  it('addresses the bridge route on the lease origin, under the app it keeps alive', () => {
    const lease = leaseFrom({ url: SRC }, SRC, ORIGIN, NOW);
    expect(keepaliveUrl(lease, 'app-a')).toBe(`${ORIGIN}/apps/app-a/.ava/keepalive`);
  });
});

describe('expiredFramePath', () => {
  it('accepts only its own app’s expiry message', () => {
    expect(expiredFramePath({ type: 'ava:embed-expired', cid: 'app-a', path: '/reports/7?x=1' }, 'app-a')).toBe('/reports/7?x=1');
    expect(expiredFramePath({ type: 'ava:embed-expired', cid: 'app-b', path: '/' }, 'app-a')).toBeNull();
    expect(expiredFramePath({ type: 'ava:theme-request' }, 'app-a')).toBeNull();
    expect(expiredFramePath('ava:embed-expired', 'app-a')).toBeNull();
    expect(expiredFramePath(null, 'app-a')).toBeNull();
  });

  it('falls back to the app root when the path is missing or not a path', () => {
    expect(expiredFramePath({ type: 'ava:embed-expired', cid: 'app-a' }, 'app-a')).toBe('/');
    expect(expiredFramePath({ type: 'ava:embed-expired', cid: 'app-a', path: 'https://elsewhere.example/' }, 'app-a')).toBe('/');
  });
});
