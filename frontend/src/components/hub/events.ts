import type { Tone } from './ui/Tile';

// Shared audit-event typing — a glyph + human label + tone per backend event
// kind, so the ledger reads like the connectors/memory lists instead of raw
// event names. Used by both Setup → History and the Data → Logs audit view, so
// the same event looks the same in both places. Tone: accent = the agent
// changed something, warn/err = permission or destructive, ok = a normal turn,
// muted = passive/system. Unknown kinds fall back to a humanised label.
export const EVENT_META: Record<string, { icon: string; label: string; tone: Tone }> = {
  turn: { icon: 'chats', label: 'Chat turn', tone: 'ok' },
  code_change: { icon: 'code', label: 'Self-edit', tone: 'accent' },
  egress: { icon: 'code', label: 'Tool call', tone: 'info' },
  memory_recall: { icon: 'db', label: 'Memory recall', tone: 'muted' },
  memory_distill: { icon: 'sparkles', label: 'Memory distilled', tone: 'accent' },
  memory_edit: { icon: 'pencil', label: 'Memory edit', tone: 'warn' },
  grant: { icon: 'lock', label: 'Permission granted', tone: 'warn' },
  revoke: { icon: 'lock', label: 'Permission revoked', tone: 'err' },
  approval: { icon: 'check', label: 'Approval', tone: 'warn' },
  route: { icon: 'activity', label: 'Intent routed', tone: 'muted' },
  job: { icon: 'image', label: 'Media job', tone: 'ok' },
  chat_delete: { icon: 'trash', label: 'Chat deleted', tone: 'err' },
  data_export: { icon: 'file', label: 'Data export', tone: 'muted' },
  data_maintenance: { icon: 'db', label: 'Data maintenance', tone: 'muted' },
};

export const humanize = (s: string) =>
  s.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());

export function eventMeta(kind: string): { icon: string; label: string; tone: Tone } {
  return EVENT_META[kind] || { icon: 'info', label: humanize(kind), tone: 'muted' };
}
