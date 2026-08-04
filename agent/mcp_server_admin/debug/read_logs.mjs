const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'read_logs',
  description: "Read systemd or application logs for debugging",
  inputSchema: {
    type: "object",
    properties: {
      source: {
        type: "string",
        enum: ["systemd", "app"],
        description: "Log source: 'systemd' for service journal, 'app' for application files"
      },
      service: {
        type: "string",
        description: "Service name for systemd (ava-bridge, vllm, etc.)"
      },
      component: {
        type: "string",
        enum: ["bridge", "learning"],
        description: "Application component for app logs"
      },
      lines: {
        type: "number",
        description: "Number of lines to return (1-500, default 50)"
      },
      level: {
        type: "string",
        enum: ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        description: "Filter by log level (optional)"
      },
      since: {
        type: "string",
        enum: ["5m", "30m", "1h", "6h", "1d", "7d"],
        description: "Time range for systemd logs (default 1h)"
      }
    },
    required: ["source"],
    additionalProperties: false
  },

  async handler(args, ctx) {
    try {
      // Use the sandbox http helper (curl-based, proxy/CA aware), NOT Node's
      // built-in fetch, which the deny-by-default network guard blocks/hangs —
      // that was the real "fetch failed" the model kept reporting.
      const data = await ctx.http.postJson(`${BRIDGE}/internal/logs`, args, {
        direct: false, timeout: 20,
        headers: { "X-Ava-Internal-Token": ctx.internalToken || "" },
      });

      if (!data || (data.ok === false && data.error)) {
        return `Error reading logs: ${(data && data.error) || "bridge unreachable"}`;
      }

      // Format output nicely
      let text = "";

      if (data.ok) {
        text += `Logs for ${data.service || data.component}\n`;
        if (data.level) text += `Level: ${data.level}\n`;
        if (data.since) text += `Since: ${data.since}\n`;
        if (data.path) text += `Path: ${data.path}\n`;
        text += `Count: ${data.count} lines\n`;
        text += `\n${"=".repeat(60)}\n\n`;
        text += data.logs.join("\n");
      } else {
        text = `Error: ${data.error}`;
      }

      return text;
    } catch (error) {
      return `Error: ${error.message}`;
    }
  },
};
