// Thin typed fetch client for the FastAPI bridge. Same-origin, so the auth
// cookie is sent automatically. A 401 means the session expired -> bounce to
// the server-rendered /login page.

import type {
  AppEntry,
  Artifact,
  ChatDetail,
  ChatSummary,
  HardwareStats,
  ImageJob,
  LearningActionResult,
  LearningState,
  LearnContext,
  ModelRoute,
  TalkResponse,
  TurnStatus,
} from './types';

function onUnauthorized() {
  window.location.href = '/login';
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin', ...init });
  if (r.status === 401) {
    onUnauthorized();
    throw new Error('unauthorized');
  }
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
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

  // Model status/control: Ava's conversational brain is the always-on open-model model.
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
  },  attachImageToChat: (id: string, url: string, caption: string) => {
    const fd = new FormData();
    fd.append('url', url);
    fd.append('caption', caption || '');
    return fetch(`/api/chats/${id}/image`, { method: 'POST', body: fd, credentials: 'same-origin' });
  },

  // Turns
  startTurn: (fd: FormData) => req<{ turn_id?: string; error?: string }>('/api/chat-stream', { method: 'POST', body: fd }),
  turn: (id: string) => req<TurnStatus>(`/api/turn/${id}`),
  weatherArtifact: (location: string, days: number) =>
    req<Artifact>(`/api/artifact/weather?location=${encodeURIComponent(location)}&days=${days}`),

  // Voice (push-to-talk): audio blob -> transcription + Ava reply + spoken WAV
  talk: (fd: FormData) => req<TalkResponse>('/api/talk', { method: 'POST', body: fd }),

  // Jobs / images
  job: (id: string) => req<ImageJob>(`/api/job/${id}`),
  cancelJob: (id: string) => req<{ ok?: boolean; job?: ImageJob; error?: string }>(`/api/job/${id}/cancel`, { method: 'POST' }),
  upscale: (filename: string) => {
    const fd = new FormData();
    fd.append('filename', filename);
    return req<{ url?: string; error?: string }>('/api/upscale', { method: 'POST', body: fd });
  },
  generate: (prompt: string, chatId: string, chatText: string) => {
    const fd = new FormData();
    fd.append('prompt', prompt);
    fd.append('chat_id', chatId);
    fd.append('chat_text', chatText);
    return req<{ job?: ImageJob; error?: string }>('/api/generate', { method: 'POST', body: fd });
  },

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

// ---- Learning ---------------------------------------------------------------
export const learning = {
  state: (ctx: LearnContext) => req<LearningState>(`/api/learning/${ctx}/state`),
  apply: (ctx: LearnContext, id: string) =>
    req<LearningActionResult>(`/api/learning/${ctx}/apply?proposal_id=${encodeURIComponent(id)}`, { method: 'POST' }),
  reject: (ctx: LearnContext, id: string) =>
    req<LearningActionResult>(`/api/learning/${ctx}/reject?proposal_id=${encodeURIComponent(id)}`, { method: 'POST' }),
  feedback: (ctx: LearnContext, id: string, rating: 0 | 1) =>
    req<LearningActionResult>(`/api/learning/${ctx}/feedback?proposal_id=${encodeURIComponent(id)}&rating=${rating}`, { method: 'POST' }),
};
