import type { Tone } from './ui/Tile';

// Shared audit-event typing — a glyph + human label + tone per backend event
// kind, so the ledger reads like the connectors/memory lists instead of raw
// event names. Used by Data → History (the ledger) and the Data → Logs tails,
// so the same event looks the same wherever it surfaces. Tone: accent = the agent
// changed something, warn/err = permission or destructive, ok = a normal turn,
// muted = passive/system. Unknown kinds fall back to a humanised label.
export const EVENT_META: Record<string, { icon: string; label: string; tone: Tone }> = {
  turn: { icon: 'chats', label: 'Chat turn', tone: 'ok' },
  egress: { icon: 'code', label: 'Tool call', tone: 'info' },
  memory_recall: { icon: 'db', label: 'Memory recall', tone: 'muted' },
  memory_distill: { icon: 'sparkles', label: 'Memory distilled', tone: 'accent' },
  memory_edit: { icon: 'pencil', label: 'Memory edit', tone: 'warn' },
  grant: { icon: 'lock', label: 'Permission granted', tone: 'warn' },
  revoke: { icon: 'lock', label: 'Permission revoked', tone: 'err' },
  approval: { icon: 'check', label: 'Approval', tone: 'warn' },
  chat_delete: { icon: 'trash', label: 'Chat deleted', tone: 'err' },
  data_export: { icon: 'file', label: 'Data export', tone: 'muted' },
  data_maintenance: { icon: 'db', label: 'Data maintenance', tone: 'muted' },
  data_delete: { icon: 'trash', label: 'Data deleted', tone: 'err' },
  model_delete: { icon: 'trash', label: 'Model deleted', tone: 'err' },
  brain_swap: { icon: 'sparkles', label: 'Brain swapped', tone: 'accent' },
  memory_delete: { icon: 'trash', label: 'Memory deleted', tone: 'err' },
  secret: { icon: 'lock', label: 'Secret changed', tone: 'warn' },
  voiceprint: { icon: 'mic', label: 'Voiceprint enrolled', tone: 'accent' },
  policy_retire: { icon: 'ghost', label: 'Policy retired', tone: 'warn' },
  connector_delete: { icon: 'trash', label: 'App removed', tone: 'err' },
  connector_prune: { icon: 'plug', label: 'App tools pruned', tone: 'warn' },
  brand_change: { icon: 'sliders', label: 'Branding changed', tone: 'accent' },
  brand_asset: { icon: 'image', label: 'Brand asset set', tone: 'accent' },
  brand_asset_delete: { icon: 'trash', label: 'Brand asset removed', tone: 'warn' },
  brand_import: { icon: 'sliders', label: 'Branding imported', tone: 'accent' },
  brand_export: { icon: 'file', label: 'Branding exported', tone: 'muted' },
  // The allocator's own ledger surfaces here too. Dotted keys are legal object
  // keys and the backend sends them verbatim; without these three the ledger
  // rendered "Alloc.lease expired", which is `humanize` doing its best with a
  // machine token nobody worded.
  'alloc.restore': { icon: 'refresh', label: 'Memory restored', tone: 'accent' },
  'alloc.lease_expired': { icon: 'gauge', label: 'Memory lease expired', tone: 'muted' },
  'alloc.breaker_reset': { icon: 'gauge', label: 'Memory guard reset', tone: 'warn' },
};

export const humanize = (s: string) =>
  s.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());

export function eventMeta(kind: string): { icon: string; label: string; tone: Tone } {
  return EVENT_META[kind] || { icon: 'info', label: humanize(kind), tone: 'muted' };
}
