import { afterEach, describe, expect, it, vi } from 'vitest';
import { appFrameDestination } from './AppFrame';

afterEach(() => vi.unstubAllGlobals());

describe('artifact app destinations', () => {
  it('keeps the isolated origin and bridge token while passing the selected dashboard', () => {
    vi.stubGlobal('window', { location: { href: 'https://ava.example/#chat' } });
    const target = new URL(appFrameDestination('https://apps.example/apps/data/?t=issued&theme=dark', 'data', '/data/collections/census?census_release=ACS+2024&theme=light&t=forged'));
    expect(target.origin).toBe('https://apps.example');
    expect(target.pathname).toBe('/apps/data/data/collections/census');
    expect(target.searchParams.get('t')).toBe('issued');
    expect(target.searchParams.get('theme')).toBe('dark');
    expect(target.searchParams.get('census_release')).toBe('ACS 2024');
  });

  it.each(['/../../api/actions', '/%2e%2e/%2e%2e/api/actions'])('refuses paths escaping the connected app: %s', path => {
    vi.stubGlobal('window', { location: { href: 'https://ava.example/#chat' } });
    expect(() => appFrameDestination('https://apps.example/apps/data/?t=issued', 'data', path)).toThrow('Invalid app destination');
  });
});
