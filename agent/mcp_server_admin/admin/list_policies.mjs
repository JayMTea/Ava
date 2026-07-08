// HOST-CALLBACK: reaches the bridge through the guard proxy via the
// host.openshell.internal alias (private-range IP in the policy allowed_ips).
// The Tailscale IP + Node fetch is blocked by the deny-by-default guard, so we
// use the curl-based ctx.http helper (proxy + TLS CA honored). Proven: proxy +
// alias = 200; direct + Tailscale IP = 403.
const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'list_policies',
  description: "List all available egress policies",
  inputSchema: {
    type: "object",
    properties: {},
    additionalProperties: false
  },

  async handler(args, ctx) {
    let data;
    try {
      data = await ctx.http.getJson(`${BRIDGE}/internal/policies`, {
        direct: false, timeout: 20,
        headers: { "X-Ava-Internal-Token": ctx.internalToken || "" },
      });
    } catch (error) {
      return `Error listing policies: ${error.message}`;
    }
    if (data && data.error) return `Error listing policies: ${data.error}`;

    let text = `Available Policies (${data.count})\n`;
    text += "=".repeat(50) + "\n\n";

    for (const policy of data.policies) {
      let marker = "";
      if (policy.protected) marker += "PROTECTED ";
      if (policy.core) marker += "CORE";

      text += `${policy.name}`;
      if (marker) text += ` [${marker}]`;
      text += `\n  Path: ${policy.path}\n\n`;
    }

    if (data.protected && data.protected.length > 0) {
      text += `\n Protected policies (cannot be modified):\n`;
      for (const p of data.protected) {
        text += `  - ${p}\n`;
      }
    }

    return text;
  },
};
