// HOST-CALLBACK through the guard proxy via host.openshell.internal (see
// list_policies.mjs). Node fetch to the Tailscale IP is blocked (403).
const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'read_config',
  description: "Read system configuration (env, persona, learning settings)",
  inputSchema: {
    type: "object",
    properties: {
      component: {
        type: "string",
        enum: ["env", "persona", "learning"],
        description: "Configuration component to read"
      }
    },
    required: ["component"],
    additionalProperties: false
  },

  async handler(args, ctx) {
    let data;
    try {
      data = await ctx.http.getJson(
        `${BRIDGE}/internal/config?component=${encodeURIComponent(args.component)}`, {
        direct: false, timeout: 20,
        headers: { "X-Ava-Internal-Token": ctx.internalToken || "" },
      });
    } catch (error) {
      return `Error reading config: ${error.message}`;
    }
    if (data && data.error) return `Error reading config: ${data.error}`;

    let text = `📋 Configuration: ${data.component}\n`;
    text += `Path: ${data.path}\n\n`;

    if (typeof data.config === "object") {
      // Env vars (dict format)
      text += "Settings:\n";
      text += "-".repeat(40) + "\n";
      for (const [key, val] of Object.entries(data.config)) {
        text += `${key}: ${val}\n`;
      }
      if (data.note) text += `\n⚠️  ${data.note}`;
    } else {
      // File content (text)
      text += data.config;
    }

    return text;
  },
};
