// Thin typed fetch client for the FastAPI bridge. Same-origin, so the auth
// cookie is sent automatically. A 401 means the session expired -> bounce to
// the server-rendered /login page.

import type {
  AppEntry,
  Artifact,
  ChatDetail,
  ChatSummary,
  HardwareStats,
  ModelRoute,
  TalkResponse,
  TurnStatus,
} from './types';

function onUnauthorized() {
  window.location.href = '/login';
}

/** The bridge does not have the route this page asked for — see `req` below.
 *  A code rather than a string test, so a surface can render the restart
 *  instruction instead of a red "something failed" it cannot act on. */
export const BRIDGE_OUTDATED = 'bridge_outdated';

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin', ...init });
  if (r.status === 401) {
    onUnauthorized();
    throw new Error('unauthorized');
  }
  if (!r.ok) {
    // Surface the server's own error message (and machine-readable code, when
    // it sends one — see features.preflight) instead of a bare status line, so
    // callers can show "voice is turned off…" + a fix-it link, not "-> 503".
    const body = await r.json().catch(() => null) as { error?: string; error_code?: string } | null;
    // A 404 on Ava's own API has two unrelated meanings, and they must not read
    // alike. EVERY bridge handler that answers 404 sends its own `{error: …}`
    // body — "no backend 'x'", "unknown connector", "not found on disk". A 404
    // with no `error` key is FastAPI's own "no route matched": the RUNNING
    // bridge has never heard of this path.
    //
    // That is version skew far more often than it is a fault. pages.py re-reads
    // frontend/dist on every request, precisely so a rebuild is picked up with
    // no restart — so a `git pull` (or `npm run build`) on a live install hands
    // the browser a NEW page that is talking to the OLD Python process, and the
    // first thing the owner sees is the newest panel failing. Reported as
    // "/api/hub/models/store -> 404" that reads as "the model store is broken",
    // and the owner goes looking for a fault in the model store. The truth is
    // "restart Ava", which is one line and one action.
    //
    // Scoped to `/api/` — Ava's own surface. `/apps/<id>/…` is a reverse proxy
    // to somebody else's server, whose 404 means their route is missing, not
    // ours, and whose body shape is not ours to interpret.
    const routeUnknown = r.status === 404 && !body?.error && path.startsWith('/api/');
    const err = new Error(
      body?.error
      || (routeUnknown
        ? `this Ava has no ${path} — the page is newer than the bridge that is `
          + 'running. Restart Ava to load it.'
        : `${path} -> ${r.status}`)) as Error & {
      code?: string; detail?: unknown;
    };
    if (body?.error_code) err.code = body.error_code;
    else if (routeUnknown) err.code = BRIDGE_OUTDATED;
    // The whole parsed body, so a caller can act on STRUCTURED detail rather
    // than only on the message. Setup → Branding needs this: a refused accent
    // comes back with the contrast ratios and a suggested replacement colour,
    // and re-deriving those client-side would mean a second implementation of
    // the OKLab walk that only the server should own.
    if (body) err.detail = body;
    throw err;
  }
  return (await r.json()) as T;
}

// Exported so the optional app overlay (frontend/src/overlay/lib/apps.ts) can
// build its own typed calls on the same fetch/auth helper.
export { req };

export const api = {
  health: () => req<{ ok: boolean; ctx_max?: number; ctx_base?: number; model?: string }>('/api/health', { cache: 'no-store' }),

  // Left-rail app registry (connectors with a `ui:` block). Drives the nav so a
  // new app appears by dropping a connector folder — no frontend edits.
  apps: () => req<{ apps: AppEntry[] }>('/api/apps'),

  // Assistant branding (name/tagline) — lets a fork re-brand without editing React.
  brand: () => req<{ name: string; tagline: string }>('/api/brand'),

  // Live device hardware snapshot (GPU util/temp, unified memory, CPU) for the
  // floating monitor bubble.
  hardware: () => req<HardwareStats>('/api/hardware', { cache: 'no-store' }),

  // Model status/control: Ava's conversational brain is whichever model the
  // owner configured (Setup -> Models); the route reports the live one.
  getModel: () => req<ModelRoute>('/api/model', { cache: 'no-store' }),
  setModel: (mode: string) => {
    const fd = new FormData();
    fd.append('mode', mode);
    return req<ModelRoute & { ok?: boolean; error?: string }>('/api/model', { method: 'POST', body: fd });
  },

  // Chats
  listChats: () => req<{ chats: ChatSummary[] }>('/api/chats'),
  getChat: (id: string) => req<ChatDetail>(`/api/chats/${id}`),
  newChat: () => req<{ id: string }>('/api/chats', { method: 'POST' }),
  deleteChat: (id: string) => req<unknown>(`/api/chats/${id}`, { method: 'DELETE' }),
  // Ghost mode: wipe the ephemeral conversation's agent-side session transcript.
  ghostDiscard: (chatId: string) => {
    const fd = new FormData();
    fd.append('chat_id', chatId);
    return fetch('/api/ghost/discard', { method: 'POST', body: fd, credentials: 'same-origin' }).catch(() => {});
  },

  // Turns. /api/chat-stream is the ONE ingress for typed messages; it answers
  // {turn_id} and the caller polls /api/turn/<id> for the reply.
  startTurn: (fd: FormData) =>
    req<{ turn_id?: string; route?: string; error?: string; error_code?: string }>(
      '/api/chat-stream', { method: 'POST', body: fd }),
  turn: (id: string) => req<TurnStatus>(`/api/turn/${id}`),
  /** Ask the bridge to stop a running turn. Always answers 200 with a verdict:
   *  "it just finished" is a race the owner cannot avoid, not an error. */
  abortTurn: (id: string) =>
    req<{ ok?: boolean; code?: string; error?: string; status?: string }>(
      `/api/turn/${id}/abort`, { method: 'POST' }),
  weatherArtifact: (location: string, days: number) =>
    req<Artifact>(`/api/artifact/weather?location=${encodeURIComponent(location)}&days=${days}`),

  // Voice (push-to-talk): audio blob -> transcription + Ava reply + spoken WAV
  talk: (fd: FormData) => req<TalkResponse>('/api/talk', { method: 'POST', body: fd }),

  // Uploads
  upload: (files: File[]) => {
    const fd = new FormData();
    files.forEach((f) => fd.append('files', f, f.name));
    return req<{ attachments: import('./types').Attachment[] }>('/api/upload', { method: 'POST', body: fd });
  },
};

export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Connected-app browser APIs live in the optional overlay
// (frontend/src/overlay/lib/apps.ts) so the core shell carries no personal-app
// calls. They import the `req` helper exported above.
