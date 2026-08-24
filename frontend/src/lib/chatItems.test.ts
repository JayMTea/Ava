import { describe, expect, it } from 'vitest';
// Vite's `?raw` rather than node:fs — the test files are typechecked by
// `tsc -b` alongside the app, and the app's tsconfig has no node types. This
// keeps the guard in the same type world as the code it guards.
import chatItemsSrc from './chatItems.ts?raw';
import chatViewSrc from '../components/chat/ChatView.tsx?raw';

// Every ChatItem kind has a renderer.
//
// The union is the chat log's whole model, and `ChatView`'s switch ends in
// `default: return null` — so a kind added to the model and forgotten in the
// switch renders NOTHING, silently. That is not a crash anybody reports; it is
// a message that just is not there. The `marker` kind was added by the gateway
// work and is exactly the shape of thing this catches.
//
// A static scan rather than a render test, because the union is a type and
// types do not exist at runtime.

function unionKinds(): string[] {
  const body = chatItemsSrc.slice(
    chatItemsSrc.indexOf('export type ChatItem'),
    chatItemsSrc.indexOf('export function uid'));
  return [...new Set([...body.matchAll(/kind:\s*'([a-z-]+)'/g)].map((m) => m[1]))];
}

describe('the chat item model', () => {
  it('the scan finds the union', () => {
    // A guard with no subjects agrees with everything.
    const kinds = unionKinds();
    expect(kinds.length).toBeGreaterThanOrEqual(5);
    expect(kinds).toContain('user');
    expect(kinds).toContain('ava');
  });

  it('every kind has a case in ChatView', () => {
    const view = chatViewSrc;
    const missing = unionKinds().filter((k) => !view.includes(`case '${k}':`));
    expect(missing, `ChatView has no case for ${missing.join(', ')} — those items `
      + 'fall through to `default: return null` and render nothing at all').toEqual([]);
  });
});
