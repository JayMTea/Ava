/** The rail, rendered.
 *
 * WHY A SNAPSHOT AND NOT A PROXY. Phase 3 promises that with the Domains
 * feature off the sidebar is byte-identical to what shipped before it. A pure
 * test over the grouping arithmetic cannot prove that: the claim is about the
 * rendered element tree, including the branch that decides whether a wrapper
 * exists at all. `react-dom/server` is already one of this app's five runtime
 * dependencies and vitest's default environment is node, so rendering it costs
 * nothing new.
 *
 * THE SEQUENCE IS THE PROOF. This file and its snapshot are written against the
 * PRE-CHANGE component and must then survive every Phase 3 edit unmodified. A
 * snapshot recorded after the change would only prove the code matches itself.
 *
 * `renderToStaticMarkup` runs no effects, so nothing fetches and no portal
 * mounts; `useBrand()` falls back to its documented default.
 */
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { RailRealms } from '../lib/realms';
import type { AppEntry, ChatSummary } from '../lib/types';
import { Drawer } from './Drawer';

// Invented ids only. Tracked source may not carry the owner's app names —
// tests/test_no_owner_identity.py scans every tracked file.
const APPS: AppEntry[] = [
  { id: 'app-a', label: 'App A', icon: 'chart', color: null, section: 'apps',
    order: 0, embed: 'iframe', url: '/apps/app-a/', has_api: true },
  { id: 'app-b', label: 'App B', icon: 'gauge', color: null, section: 'apps',
    order: 1, embed: 'iframe', url: '/apps/app-b/', has_api: false },
];

const CHATS: ChatSummary[] = [];

function render(extra: Record<string, unknown> = {}) {
  return renderToStaticMarkup(
    <Drawer
      open
      onToggle={() => {}}
      apps={APPS}
      view="chat"
      onView={() => {}}
      chats={CHATS}
      currentChatId={null}
      onNewChat={() => {}}
      onOpenChat={() => {}}
      onDeleteChat={() => {}}
      {...extra}
    />,
  );
}

describe('the rail', () => {
  it('renders both apps inside one Apps nav', () => {
    const html = render();
    expect(html).toContain('aria-label="Apps"');
    expect(html.match(/nav-item nav-app/g) ?? []).toHaveLength(APPS.length);
  });

  it('matches the committed shape', () => {
    // The baseline. Any Phase 3 change that alters the flag-off rail changes
    // this snapshot, and that is the signal.
    expect(render()).toMatchSnapshot();
  });

  it('groups by realm, and every group heading resolves', () => {
    const realms: RailRealms = {
      enabled: true,
      axis: { order: ['r1', 'r2'], labels: { r1: 'One', r2: 'Two' } },
      byApp: new Map([['app-a', ['r1']], ['app-b', ['r2', 'r1']]]),
    };
    const html = render({ realms });

    // Still ONE apps nav, and still one row per app: grouping adds structure,
    // never a row and never a second landmark.
    expect(html.match(/aria-label="Apps"/g) ?? []).toHaveLength(1);
    expect(html.match(/nav-item nav-app/g) ?? []).toHaveLength(APPS.length);

    expect(html.match(/role="group"/g) ?? []).toHaveLength(2);
    expect(html).toContain('>One<');
    expect(html).toContain('>Two<');

    // Every aria-labelledby must point at an id that exists in the same markup,
    // or the group announces unlabelled.
    for (const m of html.matchAll(/aria-labelledby="([^"]+)"/g)) {
      expect(html).toContain(`id="${m[1]}"`);
    }
  });

  it('names the other realms of a multi-realm app in its row label', () => {
    const realms: RailRealms = {
      enabled: true,
      axis: { order: ['r1', 'r2'], labels: { r1: 'One', r2: 'Two' } },
      byApp: new Map([['app-a', ['r1']], ['app-b', ['r1', 'r2']]]),
    };
    // The rail files an app under one realm only, so the row label is the one
    // channel that tells a screen-reader user it has another home.
    expect(render({ realms })).toContain('App B — checking, also in Two');
  });

});
