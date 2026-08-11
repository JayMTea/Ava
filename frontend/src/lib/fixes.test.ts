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

describe('the three codes that had nowhere to go', () => {
  // `connectors.unreachable()` emits four codes; only `_down` resolved here, so
  // a connected app that timed out or failed DNS gave the owner an accurate
  // message and no destination — which reads as a dead end, not a fixable
  // problem. All three are about ONE app's address, so they lead to that app's
  // row in Setup rather than to Operations: nothing of Ava's is down.
  it.each(['timeout', 'unreachable', 'error'])('resolves _%s to the app row', (kind) => {
    const fix = fixForCode(`my_notes_${kind}`);
    expect(fix).toBeDefined();
    expect(fix!.hash).toBe('hub/connectors');
    expect(fix!.tip).toContain('my notes');
  });

  it('still sends a down service to Operations', () => {
    expect(fixForCode('web_search_down')!.hash).toBe('ops');
  });

  it('does not hijack a code that merely ends in one of those words', () => {
    // `_off` is matched first and must stay first.
    expect(fixForCode('voice_off')!.hash).toBe('hub/system');
  });

  it('still returns nothing for a code it has never heard of', () => {
    expect(fixForCode('something_entirely_new')).toBeUndefined();
  });

  // The agent runtime is a registry capability whose switch and status live in
  // Setup → Agent, not the Optional-features list. Its codes are checked before
  // the generic patterns so `agent_off` does not send the owner hunting for a
  // checkbox that is not there, and `agent_down` does not send them to
  // Operations, which has no control for it either.
  it.each([
    ['agent_off', 'switched off'],
    ['agent_down', 'not answering'],
    ['agent_conflict', 'Direct floor'],
  ])('sends %s to Setup → Agent', (code, why) => {
    const fix = fixForCode(code);
    expect(fix).toBeDefined();
    expect(fix!.hash).toBe('hub/agent');
    expect(fix!.tip).toContain(why);
  });
});
