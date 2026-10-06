import { describe, expect, it } from 'vitest';
import { frameNavigation } from './embedNavigation';

const frame = {} as Window;
const origin = 'https://apps.example';
const event = (path: string, overrides = {}) => ({ source: frame, origin, data: { type: 'ava:navigation', cid: 'infra', path }, ...overrides }) as MessageEvent;

describe('frame navigation', () => {
  it('keeps the page, tab, detail and fragment but removes credentials and launch hints', () => {
    expect(frameNavigation(event('/machine-learning?tab=models&item=123&t=secret&embedded=1&theme=dark&access_token=secret#results'), frame, origin, 'infra'))
      .toBe('/machine-learning?tab=models&item=123#results');
  });
  it('accepts the explicit app root', () => {
    expect(frameNavigation(event('/'), frame, origin, 'infra')).toBe('/');
  });
  it.each(['/../../api/actions', '/%2e%2e/%2e%2e/api/actions', '//evil.example/', '/\\evil.example/', '/login?next=/', '/.ava/keepalive'])('rejects unsafe or transient routes: %s', path => {
    expect(frameNavigation(event(path), frame, origin, 'infra')).toBeNull();
  });
  it('keeps a credential out of the shell address, whether it rides in the query or the fragment', () => {
    // The fragment is where an implicit-flow sign-in answers, and it arrives
    // exactly when an app reports its route.
    expect(frameNavigation(event('/dashboard#access_token=ey.JJ&state=xyz'), frame, origin, 'infra'))
      .toBe('/dashboard#state=xyz');
    expect(frameNavigation(event('/callback#id_token=abc'), frame, origin, 'infra')).toBe('/callback');
    // A fragment that is a route, not a parameter set, is left exactly as it is.
    expect(frameNavigation(event('/reports#/section/2'), frame, origin, 'infra')).toBe('/reports#/section/2');
    expect(frameNavigation(event('/reports#results'), frame, origin, 'infra')).toBe('/reports#results');
  });

  it('hands back the encoding the app itself wrote, for everything it keeps', () => {
    // This string is what the frame is reopened at: re-encoding it would hand
    // the app a query it never wrote (`%20` -> `+`, `:` -> `%3A`).
    expect(frameNavigation(event('/search?q=hello%20world&theme=dark'), frame, origin, 'infra'))
      .toBe('/search?q=hello%20world');
    expect(frameNavigation(event('/f?filter=kind:pod,ns:default'), frame, origin, 'infra'))
      .toBe('/f?filter=kind:pod,ns:default');
    expect(frameNavigation(event('/p?flag'), frame, origin, 'infra')).toBe('/p?flag');
  });

  it('ignores messages from another origin, window, app or protocol', () => {
    for (const overrides of [{ origin: 'https://other.example' }, { source: {} }, { data: { type: 'ava:navigation', cid: 'other', path: '/' } }, { data: { type: 'ava:theme' } }]) {
      expect(frameNavigation(event('/', overrides), frame, origin, 'infra')).toBeNull();
    }
  });
});
