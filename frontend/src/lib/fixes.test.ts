import { describe, expect, it } from 'vitest';
import { fixForCode } from './fixes';
import { appAccent, appIcon } from './appColor';

// The first automated checks the SPA has ever had. Both targets are pure and both
// are load-bearing for a claim made elsewhere in the repo, which is why they were
// chosen over a component render test: they are exactly the places where a silent
// regression would falsify documentation rather than break a screen.

describe('fixForCode — the zero-frontend-changes contract', () => {
  // ava_bridge/features.py emits `<key>_off` / `<key>_down` and CLAUDE.md states
  // that "new capabilities need zero frontend changes" because this resolves from
  // the code PATTERN rather than a per-feature map. If someone replaces this with
  // a lookup table, that promise quietly stops being true — and the only symptom
  // is a missing fix-it link on a feature nobody has registered yet.
  it('resolves a capability it has never heard of', () => {
    const off = fixForCode('holographic_telepresence_off');
    expect(off?.hash).toBe('hub/system');
    expect(off?.tip).toContain('holographic telepresence');

    const down = fixForCode('holographic_telepresence_down');
    expect(down?.hash).toBe('ops');
    expect(down?.tip).toContain('holographic telepresence');
  });

  it('sends _off to Setup and _down to Operations', () => {
    expect(fixForCode('voice_off')?.hash).toBe('hub/system');
    expect(fixForCode('voice_down')?.hash).toBe('ops');
  });

  it('underscores become spaces in the human-facing tip, not in the route', () => {
    expect(fixForCode('learning_cloud_fallback_off')?.tip)
      .toContain('learning cloud fallback');
  });

  it('returns undefined rather than a wrong link for anything unrecognised', () => {
    for (const code of [undefined, null, '', 'nonsense', 'voice', 'off', '_off_']) {
      expect(fixForCode(code as string | null | undefined)).toBeUndefined();
    }
  });

  it('prefers _off when a code could read as both', () => {
    // '_off' is tested first, so a pathological code ending in _off wins. Pinned
    // because the ORDER is the behaviour — swapping the two blocks would silently
    // reroute these to Operations.
    expect(fixForCode('a_down_off')?.hash).toBe('hub/system');
  });
});

describe('appAccent / appIcon — stable per app id', () => {
  // CLAUDE.md: an undeclared icon comes back null on purpose so appIcon() can
  // "hash the app id into a stable glyph", and a connected app's accent must be
  // "a stable auto color". Stable means: same id, same answer, forever — a
  // reshuffle would repaint every user's sidebar on upgrade.
  it('is deterministic across calls', () => {
    for (const id of ['fitness', 'ledger', 'images', 'home-assistant']) {
      expect(appAccent(id)).toBe(appAccent(id));
      expect(appIcon(id)).toBe(appIcon(id));
    }
  });

  it('honours an explicit manifest override over the hash', () => {
    expect(appAccent({ id: 'fitness', color: 'var(--app-accent-3)' }))
      .toBe('var(--app-accent-3)');
    expect(appIcon({ id: 'fitness', icon: 'activity' })).toBe('activity');
  });

  it('falls back to a hashed value when the manifest declares nothing', () => {
    const accent = appAccent({ id: 'undeclared-app' });
    const icon = appIcon({ id: 'undeclared-app' });
    expect(accent).toBeTruthy();
    expect(icon).toBeTruthy();
    // Never the fixed backend fallback that once made every added app identical
    // (see the `or "grid"` incident recorded in CLAUDE.md).
    expect(appIcon({ id: 'a' }) === appIcon({ id: 'b' })
      && appIcon({ id: 'b' }) === appIcon({ id: 'c' })).toBe(false);
  });
});
