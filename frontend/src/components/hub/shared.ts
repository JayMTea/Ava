import type { HubConnector } from './hubApi';

// Cross-panel primitives that would otherwise force a circular import through
// HubView. Kept tiny and dependency-light on purpose.

// Setup's top-level tabs. Persona, Voice and Memory left this union when they
// became sections of Agent (see hubRoute.ts `AGENT_SUBTABS`); Budgets merged
// into Hardware and History moved to the Data page. Their old addresses are
// kept alive by the redirect tables in hubRoute.ts, not by entries here — a
// redirect keyed on a live tab would never fire.
export type TabId =
  | 'overview' | 'hardware' | 'agent' | 'connectors' | 'branding' | 'system';

// Kinds that are internal plumbing (bridge, inference router) or models (vLLM
// Omni) — they run behind the scenes / live in the Models tab, not on the
// Connectors page, which is only for external apps the user wires in.
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
