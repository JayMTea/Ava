// Every case here is an address a real bookmark, doc link or release note can
// still hold. A router that resolves one of them to the wrong tab does not
// error — it just quietly shows Overview, which reads as "the feature is gone".
import { describe, expect, it } from 'vitest';
import {
  AGENT_SUBTABS, DEFAULT_SUB, MERGED_TABS, MOVED_TABS, RELOCATED_TABS,
  hubHash, parseHubHash,
} from './hubRoute';
import type { TabId } from './shared';

const TAB_IDS: TabId[] = [
  'overview', 'hardware', 'agent', 'connectors', 'branding', 'system',
];
const parse = (h: string) => parseHubHash(h, TAB_IDS);

describe('parseHubHash', () => {
  it('lands on Overview for the bare hub address', () => {
    expect(parse('#hub')).toMatchObject({ tab: 'overview', canonical: 'hub' });
  });

  it('tolerates the slash form the router also writes', () => {
    expect(parse('#/hub/system').tab).toBe('system');
  });

  it('resolves a plain tab', () => {
    expect(parse('#hub/connectors')).toMatchObject({ tab: 'connectors', canonical: 'hub/connectors' });
  });

  it('falls back to Overview for a tab that never existed', () => {
    expect(parse('#hub/nonsense')).toMatchObject({ tab: 'overview', canonical: 'hub' });
  });

  it('ignores a stale third segment on a tab that has no sections', () => {
    expect(parse('#hub/system/junk')).toMatchObject({ tab: 'system', canonical: 'hub/system' });
  });

  it('canonicalises the spelled-out default back to bare #hub', () => {
    expect(parse('#hub/overview').canonical).toBe('hub');
  });

  // HubView is still mounted when the hash flips to another view, and its
  // listener fires on that same event. If a foreign address looked like a Setup
  // address needing canonicalisation, the handler would rewrite `#data` to
  // `#hub` and yank the user straight back out of the page they asked for.
  it('flags an address belonging to another view, so nothing canonicalises it', () => {
    for (const h of ['#vitals', '#data', '#data/history', '#chat', '#ops', '#some-app', '']) {
      expect(parse(h).foreign).toBe(true);
    }
  });

  it('never flags a Setup address as foreign', () => {
    for (const h of ['#hub', '#hub/system', '#hub/agent', '#hub/agent/voice',
      '#hub/persona', '#hub/budgets', '#hub/history', '#hub/nonsense']) {
      expect(parse(h).foreign).toBeUndefined();
    }
  });
});

describe('parseHubHash — the Agent sub-tabs', () => {
  it('lands bare #hub/agent on Runtime, so the Apply button is what you get', () => {
    expect(parse('#hub/agent')).toMatchObject({ tab: 'agent', sub: 'runtime', canonical: 'hub/agent' });
  });

  it('resolves each sub-tab', () => {
    for (const s of AGENT_SUBTABS) {
      expect(parse(`#hub/agent/${s.id}`)).toMatchObject({ tab: 'agent', sub: s.id });
    }
  });

  it('rewrites an unknown sub-tab rather than showing a blank pane', () => {
    expect(parse('#hub/agent/bogus')).toMatchObject({ sub: DEFAULT_SUB, canonical: 'hub/agent' });
  });

  it('keeps the default sub-tab out of the URL', () => {
    expect(parse(`#hub/agent/${DEFAULT_SUB}`).canonical).toBe('hub/agent');
  });
});

describe('parseHubHash — retired addresses', () => {
  it('carries a moved tab into Agent and says so in the URL', () => {
    expect(parse('#hub/persona')).toMatchObject({ tab: 'agent', sub: 'persona', canonical: 'hub/agent/persona' });
    expect(parse('#hub/voice')).toMatchObject({ tab: 'agent', sub: 'voice', canonical: 'hub/agent/voice' });
    expect(parse('#hub/memory')).toMatchObject({ tab: 'agent', sub: 'memory', canonical: 'hub/agent/memory' });
  });

  it('sends a merged tab to the tab that absorbed it', () => {
    expect(parse('#hub/budgets')).toMatchObject({ tab: 'hardware', canonical: 'hub/hardware' });
  });

  it('reports a relocated tab instead of resolving it — segment 0 is App.tsx’s', () => {
    expect(parse('#hub/history').leaveTo).toBe('data/history');
  });

  it('leaves `leaveTo` unset for every address that stays inside Setup', () => {
    for (const h of ['#hub', '#hub/agent', '#hub/agent/voice', '#hub/persona', '#hub/budgets']) {
      expect(parse(h).leaveTo).toBeUndefined();
    }
  });

  // A redirect keyed on a live tab is unreachable: the tab wins and the entry
  // is dead code that reads as working. Cheap to assert, impossible to spot.
  it('never shadows a live tab with a redirect', () => {
    for (const key of [...Object.keys(MOVED_TABS), ...Object.keys(MERGED_TABS),
      ...Object.keys(RELOCATED_TABS)]) {
      expect(TAB_IDS).not.toContain(key);
    }
  });

  it('resolves every redirect to somewhere that exists', () => {
    const subs = AGENT_SUBTABS.map((s) => s.id) as string[];
    for (const v of Object.values(MOVED_TABS)) expect(subs).toContain(v);
    for (const v of Object.values(MERGED_TABS)) expect(TAB_IDS).toContain(v);
  });
});

describe('hubHash', () => {
  it('keeps Overview and the default sub-tab bare', () => {
    expect(hubHash('overview')).toBe('hub');
    expect(hubHash('agent')).toBe('hub/agent');
    expect(hubHash('agent', DEFAULT_SUB)).toBe('hub/agent');
  });

  it('writes a named sub-tab', () => {
    expect(hubHash('agent', 'memory')).toBe('hub/agent/memory');
  });

  it('ignores a sub-tab on a tab that has none', () => {
    expect(hubHash('system', 'memory')).toBe('hub/system');
  });

  // Round-tripping is the property that actually matters: whatever we write
  // must parse back to the same place, or Back/Forward drifts a tab per press.
  it('round-trips every address it can produce', () => {
    for (const tab of TAB_IDS) {
      for (const s of AGENT_SUBTABS) {
        const written = hubHash(tab, s.id);
        const r = parse(`#${written}`);
        expect(r.canonical).toBe(written);
        if (tab === 'agent') expect(r.sub).toBe(s.id);
        else expect(r.tab).toBe(tab);
      }
    }
  });
});
