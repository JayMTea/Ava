import { describe, expect, it } from 'vitest';
import {
  AGENT_SECTIONS, agentHash, DEFAULT_SECTION, parseAgentHash, SIDE_PANELS,
} from './agentRoute';

describe('parseAgentHash', () => {
  it('bare #agent is the console with nothing selected', () => {
    const r = parseAgentHash('#agent');
    expect(r).toMatchObject({ section: 'sessions', sessionId: null, panel: null });
    expect(r.canonical).toBe('agent');
  });

  it('resolves a session and a side panel', () => {
    expect(parseAgentHash('#agent/s/abc')).toMatchObject({ sessionId: 'abc', panel: null });
    expect(parseAgentHash('#agent/s/abc/review')).toMatchObject({
      sessionId: 'abc', panel: 'review',
    });
  });

  it('a session named like a section is still reachable', () => {
    // The whole reason segment 1 is a closed vocabulary and segment 2 is free.
    expect(parseAgentHash('#agent/s/activity')).toMatchObject({
      section: 'sessions', sessionId: 'activity',
    });
  });

  it('a mistyped section falls back to the console rather than 404ing as a session', () => {
    // Without the `s/` verb this would resolve as a session id and render
    // "no such session", which reads as data loss rather than a typo.
    expect(parseAgentHash('#agent/actvity')).toMatchObject({
      section: 'sessions', sessionId: null, canonical: 'agent',
    });
  });

  it('resolves activity and the run inspector', () => {
    expect(parseAgentHash('#agent/activity')).toMatchObject({
      section: 'activity', runId: null,
    });
    expect(parseAgentHash('#agent/activity/run/r-9')).toMatchObject({
      section: 'activity', runId: 'r-9',
    });
  });

  it('resolves automations and one job', () => {
    expect(parseAgentHash('#agent/automations')).toMatchObject({
      section: 'automations', jobId: null,
    });
    expect(parseAgentHash('#agent/automations/j1')).toMatchObject({ jobId: 'j1' });
  });

  it('marks a foreign address and never offers a canonical to act on', () => {
    // AgentView stays MOUNTED after the user leaves, so without this flag it
    // would drag them back from #hub on the next hashchange.
    const r = parseAgentHash('#hub/agent/brain');
    expect(r.foreign).toBe(true);
  });

  it('canonicalisation drops what it cannot resolve and invents nothing', () => {
    expect(parseAgentHash('#agent/s/x/junk').canonical).toBe('agent/s/x');
    expect(parseAgentHash('#agent/s/').canonical).toBe('agent');
    expect(parseAgentHash('#agent/s').canonical).toBe('agent');
    expect(parseAgentHash('#agent/wat').canonical).toBe('agent');
    expect(parseAgentHash('#agent/activity/run/').canonical).toBe('agent/activity');
    expect(parseAgentHash('#agent/activity/nonsense').canonical).toBe('agent/activity');
  });

  it('tolerates trailing slashes and a missing leading hash', () => {
    expect(parseAgentHash('agent/s/x/').canonical).toBe('agent/s/x');
    expect(parseAgentHash('#/agent/s/x').canonical).toBe('agent/s/x');
  });

  it('an unknown panel is dropped, not rendered blank', () => {
    expect(parseAgentHash('#agent/s/x/nope')).toMatchObject({
      sessionId: 'x', panel: null,
    });
  });

  it('a legacy side-chat address canonicalises to the session, never dies', () => {
    // '#agent/s/x/side' shipped, then the panel was retired (read-only, it
    // duplicated the thread it sat beside). A bookmark from that era must land
    // on the session — an address that once worked never becomes a dead one.
    const r = parseAgentHash('#agent/s/x/side');
    expect(r).toMatchObject({ sessionId: 'x', panel: null });
    expect(r.canonical).toBe('agent/s/x');
    expect(SIDE_PANELS as readonly string[]).not.toContain('side');
  });
});

describe('agentHash', () => {
  it('the default section stays bare', () => {
    expect(agentHash({})).toBe('agent');
    expect(agentHash({ section: DEFAULT_SECTION })).toBe('agent');
  });

  it('round-trips every addressable form', () => {
    const forms = [
      'agent', 'agent/s/x', 'agent/s/x/files', 'agent/activity',
      'agent/activity/run/r1', 'agent/automations', 'agent/automations/j1',
    ];
    for (const f of forms) {
      expect(agentHash(parseAgentHash(`#${f}`))).toBe(f);
    }
  });

  it('every side panel is addressable', () => {
    for (const p of SIDE_PANELS) {
      expect(parseAgentHash(`#agent/s/x/${p}`).panel).toBe(p);
    }
  });

  it('every section is addressable', () => {
    for (const s of AGENT_SECTIONS) {
      expect(parseAgentHash(`#${agentHash({ section: s.id })}`).section).toBe(s.id);
    }
  });
});

describe('what is deliberately not in the address', () => {
  it('a branch is never parsed out of the URL', () => {
    // The active branch is a SERVER fact. Putting it in the address means a
    // stale bookmark silently switches your branch on open — a mutation
    // performed by a navigation, which is the worst thing an address can do.
    const r = parseAgentHash('#agent/s/x/review');
    expect(Object.keys(r)).not.toContain('branchId');
  });

  it('a split pane is not a second addressable session', () => {
    // Two ids in one address creates an unanswerable question: which one is
    // "the" session the panel segment belongs to?
    expect(parseAgentHash('#agent/s/a/s/b')).toMatchObject({ sessionId: 'a', panel: null });
  });
});
