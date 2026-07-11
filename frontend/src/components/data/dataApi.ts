// Typed client for the Data page (/api/data/*) — the on-disk store inventory.
// Facts only (paths, sizes, counts, timestamps); browsing and editing go
// through the existing governed surfaces (hubApi memory, chat endpoints).
import { req } from '../../lib/api';

export interface DataStore {
  id: string;
  label: string;
  path: string;               // AVA_HOME-relative
  format: 'sqlite' | 'json' | 'jsonl' | 'files' | 'locked';
  bytes: number;
  count: number | null;       // items, when cheap to count (null = not counted)
  last_write: number | null;  // epoch seconds
  locked: boolean;            // inventoried but never browsable (secrets)
  managed: boolean;           // rotation/retention already handles growth
  // per-store extras
  facts?: number;
  doc_chunks?: number;
  pinned?: number;
  messages?: number;
}

export interface StoresResponse {
  home: string;
  total_bytes: number;
  retention_days: number;     // 0 = keep forever
  stores: DataStore[];
}

export interface ChatRow {
  id: string;
  title: string;
  created: number;
  updated: number;
  messages: number;
  bytes: number;
}

// One record of an append-only log; fields beyond ts/kind vary per store
// (audit kinds, perf categories, device readings) so they stay open.
export interface LogEvent {
  ts: number;
  kind?: string;
  [k: string]: unknown;
}

export type LogName = 'audit' | 'performance' | 'devices';

export const dataApi = {
  stores: () => req<StoresResponse>('/api/data/stores', { cache: 'no-store' }),
  chats: () => req<{ chats: ChatRow[] }>('/api/data/chats', { cache: 'no-store' }),
  // Export is a plain download link, not a fetch: /api/data/chats/{id}/export[?format=md]
  logTail: (name: LogName, n = 100, kind = '') =>
    req<{ events: LogEvent[] }>(
      `/api/data/logs/${name}/tail?n=${n}${kind ? `&kind=${encodeURIComponent(kind)}` : ''}`,
      { cache: 'no-store' }),
};
