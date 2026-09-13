import { afterEach, describe, expect, it, vi } from 'vitest';
import { ICONS } from './icons';
import { openElsewhereActions, popupFeatures, shellUrl } from './openWindow';

afterEach(() => vi.unstubAllGlobals());

/** `key=value,flag` -> an object, the way a browser reads a features string. */
const features = (s: string) => Object.fromEntries(s.split(',').map((kv) => kv.split('=')));

describe('shellUrl', () => {
  it('is this document with the view as hash segment 0, and nothing else', () => {
    expect(shellUrl('app-a', { origin: 'https://ava.example', pathname: '/' }))
      .toBe('https://ava.example/#app-a');
  });

  it('keeps a shell served under a path prefix on that prefix', () => {
    expect(shellUrl('hub', { origin: 'https://ava.example', pathname: '/ava/' }))
      .toBe('https://ava.example/ava/#hub');
  });
});

describe('popupFeatures', () => {
  it('asks for a separate window, most of the screen, centred on it', () => {
    const f = features(popupFeatures({ availWidth: 1920, availHeight: 1080 }));
    expect(f.popup).toBe('yes');
    expect(Number(f.width)).toBe(1440); // 0.8 * 1920 = 1536, capped at the shell's useful width
    expect(Number(f.height)).toBe(918); // 0.85 * 1080
    expect(Number(f.left)).toBe(240);   // (1920 - 1440) / 2
    expect(Number(f.top)).toBe(81);     // (1080 - 918) / 2
  });

  it('shares nothing with the window that opened it', () => {
    expect(popupFeatures({ availWidth: 1920, availHeight: 1080 }).split(',')).toContain('noopener');
  });

  it('stays on the monitor the shell is on', () => {
    const f = features(popupFeatures({ availWidth: 1920, availHeight: 1080, availLeft: 1920, availTop: 0 }));
    expect(Number(f.left)).toBe(1920 + 240);
  });

  it('never asks for more than the screen has', () => {
    // Smaller than the floor on both axes: the floor gives way, not the screen.
    const f = features(popupFeatures({ availWidth: 600, availHeight: 400 }));
    expect(Number(f.width)).toBeLessThanOrEqual(600);
    expect(Number(f.height)).toBeLessThanOrEqual(400);
    expect(Number(f.left)).toBeGreaterThanOrEqual(0);
    expect(Number(f.top)).toBeGreaterThanOrEqual(0);
  });
});

describe('openElsewhereActions', () => {
  it('offers a tab and a window for every tile, naming only glyphs that exist', () => {
    // No clipboard at all: a plain-http LAN install.
    vi.stubGlobal('navigator', {});
    const acts = openElsewhereActions('app-a');
    expect(acts.map((a) => a.label)).toEqual(['Open in new tab', 'Open in new window']);
    // The same gap tests/test_icon_ssot.py closes for the nav list: a menu item
    // naming a glyph ICONS lacks renders the fallback square, silently.
    for (const a of acts) expect(Object.keys(ICONS)).toContain(a.icon);
  });

  it('adds Copy link only where the clipboard is reachable, and copies the shell address', () => {
    const writeText = vi.fn(() => Promise.resolve());
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    vi.stubGlobal('window', { location: { origin: 'https://ava.example', pathname: '/' } });
    const acts = openElsewhereActions('app-a');
    expect(acts.map((a) => a.label)).toEqual(['Open in new tab', 'Open in new window', 'Copy link']);
    expect(Object.keys(ICONS)).toContain(acts[2].icon);
    acts[2].onClick();
    expect(writeText).toHaveBeenCalledWith('https://ava.example/#app-a');
  });
});
