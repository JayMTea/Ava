// HOST-CALLBACK through the guard proxy via host.openshell.internal (see
// list_policies.mjs). Node fetch to the Tailscale IP is blocked (403).
const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'update_config',
  description: "Update system configuration with audit trail",
  inputSchema: {
    type: "object",
    properties: {
      component: {
        type: "string",
        enum: ["env", "persona", "learning"],
        description: "Configuration component to update"
      },
      updates: {
        type: "object",
        description: "Key-value pairs to update"
      },
      reason: {
        type: "string",
        description: "Reason for the change (audit trail, required)"
      }
    },
    required: ["component", "updates", "reason"],
    additionalProperties: false
  },

  async handler(args, ctx) {
    let data;
    try {
      data = await ctx.http.postJson(`${BRIDGE}/internal/config`, args, {
        direct: false, timeout: 30,
        headers: { "X-Ava-Internal-Token": ctx.internalToken || "" },
      });
    } catch (error) {
      return `Error updating config: ${error.message}`;
    }
    if (data && data.error) return `Error updating config: ${data.error}`;

    let text = `✅ Updated ${data.component}\n\n`;
    text += `Changed: ${data.changed} settings\n`;
    text += `Reason: ${data.reason}\n`;
    text += `Backup: ${data.backup}\n`;

    if (data.keys) {
      text += `\nModified keys:\n`;
      for (const key of data.keys) {
        text += `  - ${key}\n`;
      }
    }

    return text;
  },
};
