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
  it('ignores messages from another origin, window, app or protocol', () => {
    for (const overrides of [{ origin: 'https://other.example' }, { source: {} }, { data: { type: 'ava:navigation', cid: 'other', path: '/' } }, { data: { type: 'ava:theme' } }]) {
      expect(frameNavigation(event('/', overrides), frame, origin, 'infra')).toBeNull();
    }
  });
});
