// HOST-CALLBACK through the guard proxy via host.openshell.internal (see
// list_policies.mjs). Node fetch to the Tailscale IP is blocked (403).
const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'update_policy',
  description: "Update a policy by adding or removing rules",
  inputSchema: {
    type: "object",
    properties: {
      name: {
        type: "string",
        description: "Policy name to update"
      },
      updates: {
        type: "object",
        description: "Object with 'add' (rules to add) and/or 'remove' (rules to remove) arrays",
        properties: {
          add: {
            type: "array",
            description: "List of rules to add"
          },
          remove: {
            type: "array",
            description: "List of rules to remove"
          }
        }
      },
      reason: {
        type: "string",
        description: "Reason for the policy change (audit trail)"
      }
    },
    required: ["name", "updates", "reason"],
    additionalProperties: false
  },

  async handler(args, ctx) {
    let data;
    try {
      data = await ctx.http.postJson(`${BRIDGE}/internal/policies`, args, {
        direct: false, timeout: 30,
        headers: { "X-Ava-Internal-Token": ctx.internalToken || "" },
      });
    } catch (error) {
      return `Error updating policy: ${error.message}`;
    }
    if (data && data.error) return `Error updating policy: ${data.error}`;

    let text = `✅ Updated policy: ${data.name}\n\n`;
    text += `Reason: ${data.reason}\n`;
    text += `Backup: ${data.backup}\n`;
    text += `New rule count: ${data.new_rule_count}\n`;

    return text;
  },
};
