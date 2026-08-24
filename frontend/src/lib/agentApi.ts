import type { GatewayClient } from './gatewayClient';

// The typed face of the gateway's method surface.
//
// ONE file where a method name is spelled. The gateway has ~200 of them and the
// Agent tab calls most; scattering the strings across thirty components means a
// rename upstream is thirty greps, and a typo is a runtime failure in whichever
// panel nobody opened. Shaped like `components/hub/hubApi.ts` for the same
// reason it is: the call site reads as a function, not as a protocol.
//
// Nothing here catches. Callers use `useGatewayCall`, which turns a
// `GatewayCallError` into `{error, code}` — and `code` is what `lib/fixes.ts`
// resolves a guided fix from.

export interface Session {
  id: string;
  title: string;
  kind: string;
  state: 'idle' | 'queued' | 'running' | 'needs-input' | 'failed' | 'archived';
  unread: number;
  updatedAt: number;
  group?: string;
  draft?: boolean;
  branchId?: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  text: string;
  runId?: string;
  ts?: number;
  tools?: string[];
  pending?: boolean;
}

export interface RunRef { id: string; sessionId: string; at: number; status: string }

export interface FileEntry {
  path: string;
  name?: string;
  dir?: boolean;
  bytes?: number;
}

export interface AutomationJob {
  id: string;
  name: string;
  schedule: string;
  enabled: boolean;
  lastRun?: number;
  lastStatus?: string;
}

// ---------------------------------------------------------------------------
// SHAPES CAPTURED FROM A LIVE GATEWAY (OpenClaw 2026.7.1, 2026-08-23).
//
// The first version of this file was written from prose docs and got five
// method names and several parameter names wrong. They fail LOUDLY — the
// client's fail-closed capability check refuses a method the handshake never
// advertised — but only in whichever panel you happened to open, one screen at
// a time. Everything below was probed against the running gateway.
//
// Adapting here rather than in the components is the point of this file: the
// gateway's vocabulary is not Ava's, and exactly one place should know that.
// ---------------------------------------------------------------------------

/** `sessions.list` -> `sessions[]`. The addressable id is `key`
 *  ("agent:main:main"); `sessionId` is a separate uuid the chat methods do not
 *  take. `unread` is a BOOLEAN here and a count in Ava's `Session`. */
interface RawSession {
  key: string; sessionId?: string; kind?: string; chatType?: string;
  updatedAt?: number; archived?: boolean; pinned?: boolean; unread?: boolean;
  abortedLastRun?: boolean;
}

function toSession(r: RawSession): Session {
  const key = String(r.key || '');
  return {
    id: key,
    // No title field exists. The last segment of the key is what the operator
    // named the session, so it is the closest thing to one.
    title: key.split(':').pop() || key,
    kind: String(r.kind || r.chatType || 'direct'),
    state: r.archived ? 'archived' : r.abortedLastRun ? 'failed' : 'idle',
    unread: r.unread ? 1 : 0,
    updatedAt: Number(r.updatedAt || 0),
  };
}

/** A `cron.list` job row. `cron.list -> {jobs}` is verified live; the FIELD
 *  NAMES here are candidates (this box has no cron jobs to sample), so
 *  `toAutomationJob` reads several spellings and tolerates absence. Tighten to
 *  the captured shape once a real job exists. */
interface RawCronJob {
  id?: string;
  jobId?: string;
  name?: string;
  schedule?: string | { cron?: string; expr?: string };
  cron?: string;
  enabled?: boolean;
  disabled?: boolean;
  paused?: boolean;
  lastRun?: number;
  lastRunAt?: number;
  lastStatus?: string;
  lastResult?: string;
  status?: string;
}

function toAutomationJob(r: RawCronJob): AutomationJob {
  const sched = typeof r.schedule === 'string'
    ? r.schedule
    : (r.schedule?.cron || r.schedule?.expr || r.cron || '');
  // Polarity is a guess until captured: prefer an explicit positive `enabled`,
  // else invert a negative flag, else default enabled.
  const enabled = r.enabled ?? (r.disabled === true || r.paused === true ? false : true);
  return {
    id: String(r.id || r.jobId || ''),
    name: String(r.name || r.id || r.jobId || ''),
    schedule: String(sched),
    enabled,
    lastRun: r.lastRun ?? r.lastRunAt,
    lastStatus: r.lastStatus || r.lastResult || r.status,
  };
}

/** A gateway message carries `content` as typed parts, not a string. */
interface RawMessage {
  id?: string; role?: string; timestamp?: number; runId?: string;
  content?: { type?: string; text?: string }[] | string;
}

function toMessage(r: RawMessage, i: number): ChatMessage {
  const c = r.content;
  const text = typeof c === 'string'
    ? c
    : (c || []).filter((p) => p && typeof p.text === 'string')
      .map((p) => p.text).join('\n');
  const role = r.role === 'user' || r.role === 'system' ? r.role : 'assistant';
  return {
    id: String(r.id || `${r.timestamp || 0}-${i}`),
    role, text, runId: r.runId, ts: r.timestamp,
  };
}

/** `audit.list` -> `events[]`, one per state CHANGE, several per run. The
 *  Activity section lists RUNS, so events collapse by `runId` keeping the
 *  latest — otherwise one run renders as four rows. */
interface RawAuditEvent {
  eventId?: string; runId?: string; sessionKey?: string; occurredAt?: number;
  action?: string; status?: string;
}

function toRuns(events: RawAuditEvent[]): RunRef[] {
  const byRun = new Map<string, RunRef & { title?: string }>();
  for (const e of events) {
    const id = String(e.runId || e.eventId || '');
    if (!id) continue;
    const at = Number(e.occurredAt || 0);
    const prev = byRun.get(id);
    if (prev && prev.at >= at) continue;
    byRun.set(id, {
      id,
      sessionId: String(e.sessionKey || ''),
      at,
      status: String(e.status || ''),
      title: String(e.action || ''),
    });
  }
  return [...byRun.values()].sort((a, b) => b.at - a.at);
}

export function agentApi(client: GatewayClient) {
  const call = client.call.bind(client);
  // Files and the workspace are scoped by AGENT on the gateway, never by chat
  // session. Ava's panels pass a sessionId because that is what they are
  // showing; it is deliberately not forwarded (see files.list).
  const AGENT = 'main';
  return {
    system: {
      info: () => call<Record<string, unknown>>('system.info'),
      // `system.diagnostics.stability` is NOT a gateway method and never was.
      // Nothing consumed it, so it is deleted rather than redirected at
      // `system.info`, which reports host facts (arch, uptime, cpu) and not
      // findings — pointing one at the other would be a second wrong answer.
    },
    sessions: {
      list: async () => {
        const got = await call<{ sessions: RawSession[] }>('sessions.list');
        return { sessions: (got?.sessions || []).map(toSession) };
      },
      /** `id` is the session KEY. `sessions.delete` takes `key` and REFUSES
       *  both `sessionKey` and `sessionId` — verified live 2026-08-23; while
       *  this sent `sessionId`, "delete" never deleted anything. Answers
       *  {ok, key: 'agent:main:<short>', deleted}, echoing the full form. */
      remove: (id: string) =>
        call<{ ok?: boolean; key?: string; deleted?: boolean }>(
          'sessions.delete', { key: id }),
      /** `id` is the session KEY. chat.history refuses `sessionId`:
       *  "must have required property 'sessionKey'; unexpected property
       *  'sessionId'" — verified live. */
      history: async (id: string, limit = 60) => {
        const got = await call<{ messages: RawMessage[] }>('chat.history',
          { sessionKey: id, limit });
        return { messages: (got?.messages || []).map(toMessage) };
      },
    },
    // A `sessions.create` and a chat `send` wrapper used to be declared here
    // with ZERO callers — the Agent tab is read-only by design. A defined-but-
    // never-called sender is exactly where the param drift that broke the
    // Chats tab lived (it sent a `sessionId`/`text` pair the live schema
    // refuses), so both are gone rather than fixed. Sending goes through
    // POST /api/chat-stream (hooks/chatGateway.ts) — the bridge's one turn
    // pipeline — never a direct gateway call.
    models: {
      list: () => call<{ models: { id: string; name?: string }[] }>('models.list'),
      usage: () => call<Record<string, unknown>>('usage.cost'),
    },
    audit: {
      /** `audit.activity.list` does not exist; the method is `audit.list`,
       *  and it returns state-change EVENTS, not runs. */
      activity: async (limit = 100) => {
        const got = await call<{ events: RawAuditEvent[] }>(
          'audit.list', { limit });
        return { runs: toRuns(got?.events || []) };
      },
      /** No `audit.run.inspect` either. The events for one run ARE its detail,
       *  so the inspector gets them filtered rather than a purpose-built
       *  record that the gateway does not offer. */
      run: async (id: string) => {
        const got = await call<{ events: RawAuditEvent[] }>(
          'audit.list', { limit: 200 });
        const events = (got?.events || []).filter((e) => e.runId === id);
        return { runId: id, events } as Record<string, unknown>;
      },
    },
    automations: {
      /** Every scheduled job — the Automations SECTION's list.
       *
       * The SCHEDULER is `cron.list` -> {jobs}, NOT `tasks.list` (which is the
       * in-flight background-task list and always looked empty, so Automations
       * showed "nothing scheduled" on a box with real cron jobs). Verified live
       * 2026-08-24. The row FIELD NAMES are candidates, not captured — this box
       * has no cron jobs to sample — so `toAutomationJob` reads them tolerantly;
       * tighten it once a real row is captured. */
      list: async () => {
        const got = await call<{ jobs: RawCronJob[] }>('cron.list');
        return { jobs: (got?.jobs || []).map(toAutomationJob) };
      },
      runs: (id: string, limit = 20) =>
        call<{ runs: unknown[] }>('cron.runs', { id, limit }),
      run: (id: string) => call<unknown>('cron.run', { id }),
      remove: (id: string) => call<unknown>('cron.remove', { id }),
    },
    tasks: {
      /**
       * Background work belonging to ONE session — the side panel's list.
       *
       * Deliberately a separate accessor from `automations.list()` even though
       * both reach `tasks.list`: "everything scheduled" and "what this session
       * spawned" are different questions, and a shared accessor is how a panel
       * ends up answering the other one.
       */
      list: (sessionId: string) =>
        call<{ tasks: AutomationJob[] }>('tasks.list', { sessionId }),
    },
    files: {
      // The gateway's workspace is per-AGENT: every one of these takes
      // `agentId` and a bare `name`, and refuses `sessionId`/`path`
      // ("must have required property 'agentId'"). The sessionId the panels
      // pass is therefore accepted and dropped rather than removed from their
      // signatures — the files they show are the agent's, not the session's,
      // and that is a fact about the gateway, not a bug in the panel.
      list: async (_sessionId?: string) => {
        const got = await call<{ files: { name?: string; path?: string;
                                          size?: number }[] }>(
          'agents.files.list', { agentId: AGENT });
        return {
          files: (got?.files || []).map((f): FileEntry => ({
            path: String(f.name || f.path || ''),
            name: String(f.name || ''),
            bytes: f.size,
          })),
        };
      },
      read: async (_sessionId: string, name: string) => {
        const got = await call<{ file?: { content?: string; size?: number } }>(
          'agents.files.get', { agentId: AGENT, name });
        return { content: String(got?.file?.content ?? '') };
      },
      // NOTE: `agents.files.set` exposes no compare-and-swap token, so the
      // `version` the editor threads through is accepted and dropped. A write
      // therefore CAN clobber a concurrent edit; that is the gateway's
      // contract, and pretending otherwise by inventing a token would hide it.
      write: async (_sessionId: string, name: string, content: string) => {
        await call<unknown>('agents.files.set',
          { agentId: AGENT, name, content });
        return {};
      },
    },
    // TERMINAL. Params below are CAPTURED (qa/fakes/gateway-schemas.json), and
    // every one of them was previously wrong: `open` does not accept a
    // `sessionId` at all (strict schema, it takes {cols, rows} and refuses
    // unexpected props), and the handle the other calls key on is `sessionId`
    // — the terminal's OWN id — not `terminalId`, which is not a field the
    // gateway has ever had.
    //
    // What is still NOT captured is how OUTPUT arrives: `gateway.terminal.
    // enabled` is off on the sandbox these were captured against, so
    // `terminal.open` refuses and the event-vs-poll question is unanswered.
    // `terminal.text` -> {sessionId} exists and looks like a full-buffer read,
    // but "looks like" is exactly the reasoning that produced the six invented
    // names, so it is not wired on a guess. TerminalPanel gates on this.
    terminal: {
      open: (cols: number, rows: number) =>
        call<{ sessionId?: string; id?: string }>('terminal.open', { cols, rows }),
      input: (sessionId: string, data: string) =>
        call<unknown>('terminal.input', { sessionId, data }),
      resize: (sessionId: string, cols: number, rows: number) =>
        call<unknown>('terminal.resize', { sessionId, cols, rows }),
      text: (sessionId: string) =>
        call<{ text?: string }>('terminal.text', { sessionId }),
      close: (sessionId: string) => call<unknown>('terminal.close', { sessionId }),
    },
    nodes: {
      list: () => call<{ nodes: { id: string; name?: string }[] }>('node.list'),
    },
    // `plugins.list` is not a gateway method (the real ones are
    // `plugins.uiDescriptors` and `plugins.sessionAction`). Nothing called
    // this, so it is removed rather than redirected at a shape no caller
    // wants — Skills already reads its list from Ava's own provisioner.
  };
}

export type AgentApi = ReturnType<typeof agentApi>;
