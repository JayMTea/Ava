import { describe, expect, it } from 'vitest';
import { TRANSPORT_LABEL, consoleState, parseSurface } from './agentSurface';

// The console is the ENTIRE tile for an app with `ui.embed: none`, so every
// branch here is the whole product for somebody. The bug these lock down: an
// empty tool list read as "this app declares no agent actions" no matter why it
// was empty, which is how a healthy 25-tool MCP app looked misconfigured.

describe('parseSurface', () => {
  it('reads the tools, tiers and confirm flags the backend sends', () => {
    const s = parseSurface({
      tools: [{ name: 'list_docs', description: 'List them', access: 'read', confirm: false }],
      transport: 'mcp',
      source: 'live',
      error: null,
    });
    expect(s.tools).toEqual([
      { name: 'list_docs', description: 'List them', access: 'read', confirm: false },
    ]);
    expect(s.transport).toBe('mcp');
    expect(s.source).toBe('live');
    expect(s.error).toBeNull();
  });

  it('survives a response missing every optional field', () => {
    // Degrade to a plainer console, never to a crash — there is no other view
    // of this app to fall back to.
    const s = parseSurface({});
    expect(s).toEqual({ tools: [], transport: 'none', source: 'none', error: null });
    expect(parseSurface(null).tools).toEqual([]);
    expect(parseSurface('nonsense').tools).toEqual([]);
  });

  it('drops unusable rows rather than rendering blank list items', () => {
    const s = parseSurface({ tools: [{ name: '' }, null, 'x', { name: 'ok' }] });
    expect(s.tools.map((t) => t.name)).toEqual(['ok']);
  });

  it('rejects a source it does not know instead of trusting it', () => {
    expect(parseSurface({ source: 'made-up' }).source).toBe('none');
  });
});

describe('consoleState', () => {
  const base = { tools: [], transport: 'mcp', source: 'none' as const, error: null };

  it('is loading until the first answer', () => {
    expect(consoleState(null, null)).toEqual({ kind: 'loading' });
  });

  it('reports a failure of Avas own API separately from the apps', () => {
    expect(consoleState(null, 'Error: 500')).toEqual({ kind: 'unavailable', detail: 'Error: 500' });
  });

  it('lists tools when there are tools', () => {
    const tools = [{ name: 'a' }, { name: 'b' }];
    expect(consoleState({ ...base, tools, source: 'live' }, null)).toEqual({
      kind: 'tools', tools, stale: false, detail: null,
    });
  });

  it('marks a cached list stale and carries the reason it could not refresh', () => {
    const tools = [{ name: 'a' }];
    expect(consoleState({ ...base, tools, source: 'cache', error: 'connection refused' }, null))
      .toEqual({ kind: 'tools', tools, stale: true, detail: 'connection refused' });
  });

  // The three empty-list cases. Collapsing any two of them is the original bug.
  it('separates an app that could not be asked from one with nothing to offer', () => {
    expect(consoleState({ ...base, error: 'connection refused' }, null))
      .toEqual({ kind: 'unreachable', detail: 'connection refused' });
  });

  it('says so when the app answered and genuinely lists nothing', () => {
    expect(consoleState({ ...base, source: 'live' }, null)).toEqual({ kind: 'silent' });
  });

  it('only claims "declares no agent actions" when the manifest declares none', () => {
    expect(consoleState({ ...base, transport: 'none' }, null)).toEqual({ kind: 'none' });
  });

  it('does not mistake "declares no agent surface" for an outage', () => {
    // The backend sets `error` in this case too; transport decides, not error.
    const s = { ...base, transport: 'none', error: 'this connector declares no agent surface' };
    expect(consoleState(s, null)).toEqual({ kind: 'none' });
  });
});

describe('TRANSPORT_LABEL', () => {
  it('covers every transport the backend can report', () => {
    // connectors.TRANSPORTS — a new one must not render as `undefined`.
    for (const t of ['mcp', 'discover', 'rest', 'none']) {
      expect(TRANSPORT_LABEL[t]).toBeTypeOf('string');
    }
  });
});
