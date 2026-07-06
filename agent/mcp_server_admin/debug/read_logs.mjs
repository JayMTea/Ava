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
        description: "Service name for systemd (ava-bridge, ava-gpusvc, vllm, etc.)"
      },
      component: {
        type: "string",
        enum: ["bridge", "gpusvc", "learning"],
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
      const response = await fetch(`${BRIDGE}/internal/logs`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Ava-Internal-Token": ctx.internalToken || "",
        },
        body: JSON.stringify(args),
      });

      const data = await response.json();

      if (!response.ok) {
        return `Error reading logs: ${data.error || response.statusText}`;
      }

      // Format output nicely
      let text = "";

      if (data.ok) {
        text += `📋 Logs for ${data.service || data.component}\n`;
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
