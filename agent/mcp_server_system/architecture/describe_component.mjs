// describe_component — look up one part of Ava's architecture in detail.
//
// HOST-CALLBACK: asks the bridge (over the ava-knowledge /internal egress) for
// the manifest record of a single service, MCP tool, capability category, layer,
// or egress policy by name/id.

const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'describe_component',
  description: "Get the details of ONE part of Ava's architecture by name — a "
    + "service (e.g. 'ava-bridge'), an MCP tool (e.g. 'run_gpu_job'), a "
    + "capability category (e.g. 'persona'), a layer (e.g. 'control'), or an "
    + "egress policy (e.g. 'ava-weather'). Use get_architecture first to see the "
    + "available names. Returns that component's manifest record.",
  inputSchema: {
    type: 'object',
    properties: {
      name: { type: 'string', description: "The component's id/name (service, tool, category, layer, or policy)." },
    },
    required: ['name'],
  },
  handler: async (args, { http, internalToken }) => {
    const name = args && args.name && String(args.name).trim();
    if (!name) throw new Error('name is required (see get_architecture for the available names)');
    const r = await http.postJson(`${BRIDGE}/internal/architecture/describe`, { name }, {
      direct: false, timeout: 30,
      headers: { 'X-Ava-Internal-Token': internalToken },
    });
    if (r && r.error) return r.error;
    return `${name} →\n${JSON.stringify(r, null, 2)}`;
  },
};
