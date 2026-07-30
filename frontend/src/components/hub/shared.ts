import type { HubConnector } from './hubApi';

// Cross-panel primitives that would otherwise force a circular import through
// HubView. Kept tiny and dependency-light on purpose.

export type TabId =
  | 'overview' | 'hardware' | 'agent' | 'connectors'
  | 'voice' | 'persona' | 'memory' | 'budgets' | 'history' | 'system';

// Kinds that are internal plumbing (bridge, inference router) or models (vLLM
// Omni, the GPU service) — they run behind the scenes / live in the Models tab, not on
// the Connectors page, which is only for external apps the user wires in.
export const INTERNAL_KINDS = new Set(['core', 'inference', 'media']);
export const isExternalApp = (c: HubConnector): boolean => !INTERNAL_KINDS.has(c.kind);

export type ConnectorGroup = 'devices' | 'apps' | 'tools';

/**
 * Which section of Setup → Connectors a connector belongs in.
 *
 * Groups by what the connector IS to its owner, never by how its tools travel.
 * Those are orthogonal axes, and conflating them is what made the old APP / MCP
 * badges meaningless: a device can speak MCP, an app can expose plain REST, and
 * a pure tool server has no UI at all. Identity decides the section; the wire
 * protocol is shown separately in the row's meta line (see TRANSPORT_LABEL).
 *
 * Precedence is device > app > tools: a connector with a device role is a device
 * to its owner even when it also renders a UI tile.
 */
export function connectorGroup(c: HubConnector): ConnectorGroup {
  if (c.kind === 'device') return 'devices';
  if (c.app) return 'apps';
  return 'tools';
}
