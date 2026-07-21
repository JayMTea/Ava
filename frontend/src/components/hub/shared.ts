import type { HubConnector } from './hubApi';

// Cross-panel primitives that would otherwise force a circular import through
// HubView. Kept tiny and dependency-light on purpose.

export type TabId =
  | 'overview' | 'hardware' | 'agent' | 'connectors'
  | 'voice' | 'memory' | 'budgets' | 'history' | 'system';

// Kinds that are internal plumbing (bridge, inference router) or models (vLLM
// Omni, the GPU service) — they run behind the scenes / live in the Models tab, not on
// the Connectors page, which is only for external apps the user wires in.
export const INTERNAL_KINDS = new Set(['core', 'inference', 'media']);
export const isExternalApp = (c: HubConnector): boolean => !INTERNAL_KINDS.has(c.kind);
