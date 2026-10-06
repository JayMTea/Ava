// get_active_model — Ava can tell which LLM brain is currently live.
//
// The sandbox does not reach vLLM directly. It asks the host bridge's scoped
// /internal/model route, so this tool inherits the same egress policy and
// internal-token enforcement as Ava's other host callbacks.

const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'get_active_model',
  description: 'Report Ava\'s active local LLM route and the most recent reply metadata from the configured model. Read-only.',
  inputSchema: {
    type: 'object',
    properties: {},
  },
  handler: async (_args, { http, internalToken }) => {
    try {
      const route = await http.getJson(`${BRIDGE}/internal/model`, {
        direct: false,
        timeout: 10,
        headers: { 'X-Ava-Internal-Token': internalToken },
      });
      return route;
    } catch (error) {
      return { error: `Failed to read active model: ${error.message}` };
    }
  },
};
