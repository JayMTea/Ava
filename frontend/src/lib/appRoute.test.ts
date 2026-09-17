import { describe, expect, it } from 'vitest';
import { appRouteFromHash } from './appRoute';

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
