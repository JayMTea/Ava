import { describe, expect, it } from 'vitest';
import { frameShowsSources } from './embedSources';

const frame = {} as Window;
const origin = 'https://apps.example';
const event = (overrides = {}) => ({ source: frame, origin, data: { type: 'ava:sources-in-view' }, ...overrides }) as MessageEvent;

describe('a view that shows its own sources', () => {
  it('is believed from its own frame on the app origin', () => {
    expect(frameShowsSources(event(), frame, origin)).toBe(true);
  });

  it('is ignored from another window, another origin, or under another type', () => {
    expect(frameShowsSources(event({ source: {} as Window }), frame, origin)).toBe(false);
    expect(frameShowsSources(event({ origin: 'https://evil.example' }), frame, origin)).toBe(false);
    expect(frameShowsSources(event({ data: { type: 'ava:navigation', cid: 'x', path: '/' } }), frame, origin)).toBe(false);
    expect(frameShowsSources(event({ data: null }), frame, origin)).toBe(false);
  });

  it('is ignored before the frame exists', () => {
    expect(frameShowsSources(event({ source: null }), undefined, origin)).toBe(false);
  });
});
