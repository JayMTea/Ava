// HOST-CALLBACK through the guard proxy via host.openshell.internal (see
// list_policies.mjs). Node fetch to the Tailscale IP is blocked (403).
const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'read_policy',
  description: "Read a specific policy file",
  inputSchema: {
    type: "object",
    properties: {
      name: {
        type: "string",
        description: "Policy name (without .yaml extension)"
      }
    },
    required: ["name"],
    additionalProperties: false
  },

  async handler(args, ctx) {
    let data;
    try {
      data = await ctx.http.getJson(
        `${BRIDGE}/internal/policies/${encodeURIComponent(args.name)}`, {
        direct: false, timeout: 20,
        headers: { "X-Ava-Internal-Token": ctx.internalToken || "" },
      });
    } catch (error) {
      return `Error reading policy: ${error.message}`;
    }
    if (data && data.error) return `Error reading policy: ${data.error}`;

    let text = `📋 Policy: ${data.name}\n`;
    if (data.protected) text += "🔒 PROTECTED (read-only)\n";
    text += `Path: ${data.path}\n\n`;

    text += "---YAML---\n";
    text += JSON.stringify(data.policy, null, 2);

    return text;
  },
};
