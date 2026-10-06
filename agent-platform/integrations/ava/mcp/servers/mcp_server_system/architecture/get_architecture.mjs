// get_architecture — read Ava's own live system architecture (the SSOT).
//
// HOST-CALLBACK: the architecture manifest, diagrams, and drift report live on
// the host. This asks the bridge (over the ava-knowledge /internal egress) for a
// full snapshot AND the exact manifest YAML, so Ava can answer any question
// about her own system and, if needed, edit it with update_architecture.

const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'get_architecture',
  description: "Read Ava's own system architecture — the single source of truth "
    + "(SSOT) that the diagrams and docs are generated from. Call this whenever "
    + "the user asks how the system, services, ports, tools, capabilities, layers, or "
    + "diagrams work, or before changing them. Returns the layers, services + "
    + "ports, MCP tool capabilities, egress policies, a drift report (whether the "
    + "manifest matches the running code), and the raw architecture.yaml so you "
    + "can edit it. No arguments.",
  inputSchema: { type: 'object', properties: {} },
  handler: async (_args, { http, internalToken }) => {
    const r = await http.getJson(`${BRIDGE}/internal/architecture`, {
      direct: false, timeout: 30,
      headers: { 'X-Ava-Internal-Token': internalToken },
    });
    const s = (r && r.summary) || {};
    if (!s.meta) return `Could not read architecture: ${JSON.stringify(r).slice(0, 300)}`;
    const c = s.counts || {};
    const svc = (s.services || []).map(x =>
      `  - ${x.id}: ${x.bind || '—'}${x.public && x.public !== '—' ? ` (public ${x.public})` : ''}`).join('\n');
    const caps = (s.capabilities || []).map(x => `  - ${x.category}: ${x.tools.join(', ')}`).join('\n');
    const d = s.drift || {};
    const drift = d.ok
      ? 'in sync (manifest matches the running code)'
      : `DRIFT — ${(d.errors || []).length} error(s):\n    ${(d.errors || []).join('\n    ')}`;
    const warn = (d.warnings || []).length ? `\n  warnings:\n    ${d.warnings.join('\n    ')}` : '';
    return [
      `Ava architecture — ${s.meta.name} on ${s.meta.host} (updated ${s.meta.updated})`,
      `Layers (${c.layers}): ${(s.layers || []).map(l => l.title).join('  ›  ')}`,
      `Services (${c.services}):\n${svc}`,
      `Capabilities (${c.capabilities} tools):\n${caps}`,
      `Policies (${c.policies}): ${(s.policies || []).join(', ')}`,
      `Diagrams: ${(s.diagrams || []).join(', ')}`,
      `Drift: ${drift}${warn}`,
      '',
      'Raw manifest (architecture.yaml) follows — edit and submit it via update_architecture:',
      '```yaml',
      (r.manifest_yaml || '').trimEnd(),
      '```',
    ].join('\n');
  },
};
