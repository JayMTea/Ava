import { describe, expect, it } from 'vitest';
import type { Session } from '../../lib/agentApi';
import {
  busyCount, chatIdOfSession, CHATS_GROUP_ID, groupConsoleSessions,
  groupRunsByDay, groupSessions, prChipView, receiptBadge,
  RUN_TONE, runTone, SESSION_LABEL, SESSION_TONE, sessionIcon, unreadLabel,
  unreadRollup,
} from './agentView';

const s = (over: Partial<Session> = {}): Session => ({
  id: 'x', title: 't', kind: 'other', state: 'idle', unread: 0,
  updatedAt: 0, ...over,
});

describe('the session vocabulary', () => {
  it('every state has a tone and a label', () => {
    // Exhaustive over the union: a state added to agentApi without copy here
    // renders as `undefined` in the DOM, which is a blank pill.
    const states: Session['state'][] = [
      'idle', 'queued', 'running', 'needs-input', 'failed', 'archived',
    ];
    for (const st of states) {
      expect(SESSION_TONE[st], `tone for ${st}`).toBeTruthy();
      expect(SESSION_LABEL[st], `label for ${st}`).toBeTruthy();
    }
  });

  it('uses only the six tones the stylesheet defines', () => {
    // test_hub_uniformity.py asserts exactly six `.tone-*` setters. A seventh
    // name here renders an uncoloured dot and fails nothing at build time.
    const allowed = new Set(['muted', 'accent', 'ok', 'warn', 'err', 'info']);
    for (const t of Object.values(SESSION_TONE)) expect(allowed).toContain(t);
  });

  it('running is not "ok"', () => {
    // `ok` reads as "finished well"; a running session has not finished.
    expect(SESSION_TONE.running).not.toBe('ok');
    expect(SESSION_TONE.failed).toBe('err');
  });

  it('falls back to a real icon for an unknown kind', () => {
    expect(sessionIcon('coding')).toBe('code');
    expect(sessionIcon('something-new')).toBe('chats');
  });
});

describe('groupSessions', () => {
  it('buckets by kind in the upstream order', () => {
    const got = groupSessions([
      s({ id: 'a', kind: 'other' }),
      s({ id: 'b', kind: 'coding' }),
      s({ id: 'c', kind: 'group' }),
    ]);
    expect(got.map((g) => g.id)).toEqual(['coding', 'group', 'other']);
  });

  it('omits an empty group rather than heading a void', () => {
    const got = groupSessions([s({ kind: 'coding' })]);
    expect(got).toHaveLength(1);
    expect(got[0].id).toBe('coding');
  });

  it('a custom group wins over kind and sorts after the built-ins', () => {
    const got = groupSessions([
      s({ id: 'a', kind: 'coding' }),
      s({ id: 'b', kind: 'coding', group: 'Ops' }),
    ]);
    expect(got.map((g) => g.id)).toEqual(['coding', 'Ops']);
  });

  it('excludes archived sessions', () => {
    // Filtered HERE, not by each caller: one that forgot would show a list that
    // quietly grows forever.
    const got = groupSessions([s({ state: 'archived' }), s({ id: 'b' })]);
    expect(got.flatMap((g) => g.sessions).map((x) => x.id)).toEqual(['b']);
  });

  it('sorts most-recent first inside a group', () => {
    const got = groupSessions([
      s({ id: 'old', updatedAt: 10 }), s({ id: 'new', updatedAt: 99 }),
    ]);
    expect(got[0].sessions.map((x) => x.id)).toEqual(['new', 'old']);
  });

  it('does not float unread to the top', () => {
    // A list that reorders itself while you read it is one you lose your place
    // in. Unread is a dot, not a sort key.
    const got = groupSessions([
      s({ id: 'recent', updatedAt: 99 }),
      s({ id: 'unread', updatedAt: 1, unread: 9 }),
    ]);
    expect(got[0].sessions[0].id).toBe('recent');
  });

  it('handles an empty list without inventing groups', () => {
    expect(groupSessions([])).toEqual([]);
  });
});

describe('chatIdOfSession', () => {
  it('round-trips the chat id out of both key forms', () => {
    // sessions.list and chat events carry the full 'agent:main:<short>' form;
    // the bridge itself mints the short one. Both must resolve identically.
    expect(chatIdOfSession('agent:main:ava-phone-c1', 'ava-phone')).toBe('c1');
    expect(chatIdOfSession('ava-phone-c1', 'ava-phone')).toBe('c1');
  });

  it('is null for agent-native sessions and the bare prefix', () => {
    expect(chatIdOfSession('agent:main:main', 'ava-phone')).toBeNull();
    // The bare prefix session is Ava's, but it is not A CHAT — labeling it
    // 'Background' is groupConsoleSessions' job, not this selector's.
    expect(chatIdOfSession('agent:main:ava-phone', 'ava-phone')).toBeNull();
    expect(chatIdOfSession('agent:main:ava-phone-', 'ava-phone')).toBeNull();
  });

  it('requires the separator dash, not a shared spelling', () => {
    // 'ava-phone2-c1' shares nine characters with the prefix and is somebody
    // else's session; a bare startsWith(prefix) would claim it.
    expect(chatIdOfSession('agent:main:ava-phone2-c1', 'ava-phone')).toBeNull();
  });

  it('respects a reconfigured prefix rather than hardcoding the default', () => {
    // AVA_OC_SESSION is env-configurable; the frontend learns the value from
    // /api/gateway/status session_prefix.
    expect(chatIdOfSession('agent:main:custom-c9', 'custom')).toBe('c9');
    expect(chatIdOfSession('agent:main:ava-phone-c9', 'custom')).toBeNull();
    expect(chatIdOfSession('agent:main:ava-phone-c9', '')).toBeNull();
  });
});

describe('groupConsoleSessions', () => {
  const P = 'ava-phone';
  const chats = [{ id: 'c1', title: 'Trip planning' }, { id: 'c2' }];

  it('puts chat-origin sessions under "Your chats" wearing the REAL title', () => {
    const got = groupConsoleSessions([
      s({ id: 'agent:main:ava-phone-c1', title: 'ava-phone-c1' }),
      s({ id: 'agent:main:main', title: 'main', kind: 'coding' }),
    ], P, chats);
    expect(got.map((g) => g.id)).toEqual([CHATS_GROUP_ID, 'coding']);
    expect(got[0].label).toBe('Your chats');
    expect(got[0].sessions[0]).toMatchObject({
      title: 'Trip planning', chatId: 'c1', icon: 'chats',
    });
    expect(got[0].sessions[0].note).toBeUndefined();
  });

  it('falls back to the cid and says so when the chat was deleted', () => {
    const got = groupConsoleSessions(
      [s({ id: 'agent:main:ava-phone-gone', title: 'ava-phone-gone' })], P, chats);
    expect(got[0].sessions[0]).toMatchObject({
      title: 'gone', note: 'deleted from Chats', chatId: 'gone',
    });
  });

  it('an untitled live chat reads like the sidebar, not like a key', () => {
    // The Recents list calls a title-less chat 'New chat'; two surfaces naming
    // the same conversation differently reads as two conversations.
    const got = groupConsoleSessions(
      [s({ id: 'agent:main:ava-phone-c2' })], P, chats);
    expect(got[0].sessions[0].title).toBe('New chat');
    expect(got[0].sessions[0].note).toBeUndefined();
  });

  it('titles the bare prefix session Background', () => {
    // Voice warm-ups and turns without a chat land there, and its raw key is
    // the one an owner is guaranteed to meet and least able to interpret.
    const got = groupConsoleSessions(
      [s({ id: 'agent:main:ava-phone', title: 'ava-phone' })], P, chats);
    expect(got[0].id).toBe(CHATS_GROUP_ID);
    expect(got[0].sessions[0]).toMatchObject({ title: 'Background', icon: 'chats' });
    expect(got[0].sessions[0].chatId).toBeUndefined();
  });

  it('leaves agent-native sessions in the existing groups, icon untouched', () => {
    const got = groupConsoleSessions([
      s({ id: 'agent:main:build', kind: 'coding' }),
      s({ id: 'agent:main:other-thing', kind: 'other' }),
    ], P, chats);
    expect(got.map((g) => g.id)).toEqual(['coding', 'other']);
    for (const row of got.flatMap((g) => g.sessions)) {
      expect(row.icon).toBeUndefined();
    }
  });

  it('sorts Your chats most-recent first, like every other group', () => {
    const got = groupConsoleSessions([
      s({ id: 'agent:main:ava-phone-old', updatedAt: 10 }),
      s({ id: 'agent:main:ava-phone-new', updatedAt: 99 }),
    ], P, chats);
    expect(got[0].sessions.map((x) => x.chatId)).toEqual(['new', 'old']);
  });

  it('excludes archived chat sessions like everything else', () => {
    const got = groupConsoleSessions(
      [s({ id: 'agent:main:ava-phone-c1', state: 'archived' })], P, chats);
    expect(got).toEqual([]);
  });
});

describe('rollups', () => {
  it('counts unread across live sessions only', () => {
    expect(unreadRollup([
      s({ unread: 2 }), s({ unread: 3 }), s({ unread: 5, state: 'archived' }),
    ])).toBe(5);
  });

  it('ignores a negative unread rather than subtracting', () => {
    expect(unreadRollup([s({ unread: -4 }), s({ unread: 1 })])).toBe(1);
  });

  it('busy counts running and queued', () => {
    expect(busyCount([
      s({ state: 'running' }), s({ state: 'queued' }), s({ state: 'idle' }),
    ])).toBe(2);
  });

  it('a bare dot gets a label a screen reader can read', () => {
    expect(unreadLabel(3)).toBe('3 unread');
    expect(unreadLabel(0)).toBe('');
  });
});

describe('prChipView', () => {
  it('CI drives the tone, because that is what you glance for', () => {
    expect(prChipView({ ci: 'passing' }).tone).toBe('ok');
    expect(prChipView({ ci: 'failing' }).tone).toBe('err');
    expect(prChipView({ ci: 'pending' }).tone).toBe('warn');
  });

  it('no CI result is muted, not green', () => {
    // "no result" and "passed" are different facts.
    expect(prChipView({ number: 4 }).tone).toBe('muted');
  });

  it('renders the number and the diff', () => {
    expect(prChipView({ number: 482, additions: 124, deletions: 18 }).label)
      .toBe('#482 +124 −18');
  });

  it('omits the diff when there is none rather than showing +0 −0', () => {
    expect(prChipView({ number: 1 }).label).toBe('#1');
  });

  it('puts repo, branch and state in the title', () => {
    const v = prChipView({ repo: 'ava', branch: 'main', state: 'open', ci: 'passing' });
    expect(v.title).toContain('ava');
    expect(v.title).toContain('main');
    expect(v.title).toContain('CI passing');
  });
});


describe('groupRunsByDay', () => {
  const NOW = new Date('2026-03-10T12:00:00').getTime();
  const day = (iso: string) => new Date(iso).getTime();

  it('names today and yesterday against the VIEWER\'s clock', () => {
    // A page left open overnight would otherwise keep calling yesterday
    // "Today" — which is why `now` is a parameter rather than a closure.
    const got = groupRunsByDay([
      { id: 'a', at: day('2026-03-10T09:00:00') },
      { id: 'b', at: day('2026-03-09T09:00:00') },
      { id: 'c', at: day('2026-03-01T09:00:00') },
    ], NOW);
    expect(got.map((g) => g.label)).toEqual(['Today', 'Yesterday', '2026-03-01']);
  });

  it('the same list reads differently a day later', () => {
    const runs = [{ id: 'a', at: day('2026-03-10T09:00:00') }];
    expect(groupRunsByDay(runs, NOW)[0].label).toBe('Today');
    expect(groupRunsByDay(runs, NOW + 86_400_000)[0].label).toBe('Yesterday');
  });

  it('newest day first, and newest run first inside it', () => {
    const got = groupRunsByDay([
      { id: 'early', at: day('2026-03-10T08:00:00') },
      { id: 'late', at: day('2026-03-10T18:00:00') },
    ], NOW);
    expect(got[0].runs.map((r) => r.id)).toEqual(['late', 'early']);
  });

  it('keeps an undated run instead of dropping it', () => {
    // Silently hiding a row because one field is missing is how an audit view
    // quietly stops being one.
    const got = groupRunsByDay([{ id: 'x' }, { id: 'y', at: NOW }], NOW);
    expect(got[got.length - 1]).toMatchObject({ key: 'undated' });
    expect(got.flatMap((g) => g.runs)).toHaveLength(2);
  });

  it('an empty list makes no groups', () => {
    expect(groupRunsByDay([], NOW)).toEqual([]);
  });
});

describe('run tones', () => {
  it('a finished run may read as ok — runs are past tense', () => {
    expect(runTone('done')).toBe('ok');
    expect(runTone('failed')).toBe('err');
    expect(runTone('running')).toBe('accent');
  });

  it('an unknown status is muted rather than green', () => {
    expect(runTone('something-new')).toBe('muted');
    expect(runTone(undefined)).toBe('muted');
  });

  it('uses only the six tones the stylesheet defines', () => {
    const allowed = new Set(['muted', 'accent', 'ok', 'warn', 'err', 'info']);
    for (const t of Object.values(RUN_TONE)) expect(allowed).toContain(t);
  });
});

describe('receiptBadge', () => {
  it('distinguishes a decision that GATED something from one merely recorded', () => {
    // Showing them identically lets a reader conclude a policy was applied when
    // it was only noted — the most misleading thing an audit view can do.
    expect(receiptBadge({ enforced: true }).label).toBe('enforced');
    expect(receiptBadge({ enforced: false }).label).toBe('attribution only');
    expect(receiptBadge({}).label).toBe('attribution only');
  });

  it('does not dress an un-enforced receipt in a confident tone', () => {
    expect(receiptBadge({}).tone).toBe('muted');
  });
});
