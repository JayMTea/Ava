import { describe, expect, it } from 'vitest';
import { appPathsFrom, appRouteFromHash, appRouteIsExplicit } from './appRoute';

describe('app deep links', () => {
  it('preserves the model query and prediction product destination', () => {
    expect(appRouteFromHash('#infra/machine-learning?model=123')).toEqual({
      view: 'infra', path: '/machine-learning?model=123',
    });
    expect(appRouteFromHash('#/labdash/predictions/456?release=789')).toEqual({
      view: 'labdash', path: '/predictions/456?release=789',
    });
  });
  it('keeps existing tile bookmarks', () => {
    expect(appRouteFromHash('#infra')).toEqual({ view: 'infra', path: '/' });
    expect(appRouteFromHash('#chat')).toEqual({ view: 'chat', path: '/' });
  });
  it.each(['', '#', '#../chat', '#infra/../../chat', '#infra/%2e%2e/chat'])(
    'rejects an invalid or escaped destination: %s', (hash) => {
      expect(appRouteFromHash(hash)).toBeNull();
    },
  );
});

describe('an explicit destination', () => {
  it('separates "open the app" from "open the app at its home"', () => {
    // A bare tile must not overwrite where the app was left; a trailing slash
    // is the shell saying home, and must.
    expect(appRouteIsExplicit('#infra')).toBe(false);
    expect(appRouteIsExplicit('#/infra')).toBe(false);
    expect(appRouteIsExplicit('#infra/')).toBe(true);
    expect(appRouteIsExplicit('#infra/machine-learning?tab=experiments')).toBe(true);
  });
  it('is never claimed for a fragment that is not a route at all', () => {
    for (const hash of ['', '#', '#../chat', '#infra/../../chat']) {
      expect(appRouteIsExplicit(hash)).toBe(false);
    }
  });
});

describe('the remembered app paths', () => {
  it('keeps app-relative destinations', () => {
    expect(appPathsFrom({ infra: '/machine-learning?tab=models', labdash: '/' }))
      .toEqual({ infra: '/machine-learning?tab=models', labdash: '/' });
  });
  it('drops anything that would not survive appFrameDestination', () => {
    expect(appPathsFrom({
      infra: 'machine-learning',            // not app-relative
      a: '//evil.example/',                 // protocol-relative
      b: '/../../etc/passwd',               // escapes the app prefix
      c: '/win\\path',                      // backslash
      d: 42,                                // not a string
      'bad id': '/ok',                      // not an app id
    })).toEqual({});
    expect(appPathsFrom(null)).toEqual({});
    expect(appPathsFrom(['/machine-learning'])).toEqual({});
  });
  it('prunes to the apps that still exist when the list is known', () => {
    const stored = { infra: '/machine-learning', retired: '/somewhere' };
    expect(appPathsFrom(stored, ['infra', 'labdash'])).toEqual({ infra: '/machine-learning' });
    // Unknown list: keep everything, because at boot the list is simply not in yet.
    expect(appPathsFrom(stored)).toEqual(stored);
  });
});
