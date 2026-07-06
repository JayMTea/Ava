const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'read_performance',
  description:
    "Analyse Ava's generation performance across all her apps. Reads the " +
    "append-only performance logs written by the Ava bridge/router (LLM " +
    "tokens/sec, TTFT, sampling params, which model served, failovers) and any " +
    "connected apps that write a performance.jsonl (image/video render seconds + " +
    "steps/sec, tokens/sec). Use when the user asks how fast Ava is generating, " +
    "to compare models, or to spot throughput regressions.",
  inputSchema: {
    type: "object",
    properties: {
      app: {
        type: "string",
        description: "Restrict to one app by its perf-source key (e.g. 'ava'); omit for all."
      },
      category: {
        type: "string",
        enum: ["llm", "image", "video", "upscale"],
        description: "Restrict to one generation type (default: all)"
      },
      since: {
        type: "string",
        description: "Time window like '30m', '6h', '2d' (default: all time)"
      },
      limit: {
        type: "number",
        description: "How many most-recent records to return verbatim (1-500, default 50)"
      },
      summary: {
        type: "boolean",
        description: "Include the aggregate throughput summary (default true)"
      }
    },
    additionalProperties: false
  },

  async handler(args, ctx) {
    try {
      const response = await fetch(`${BRIDGE}/internal/perf`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Ava-Internal-Token": ctx.internalToken || "",
        },
        body: JSON.stringify(args || {}),
      });

      const data = await response.json();
      if (!response.ok || !data.ok) {
        return `Error reading performance: ${data.error || response.statusText}`;
      }

      let text = `📈 Generation performance — ${data.apps.join(", ")}\n`;
      text += `Total records: ${data.total}\n`;
      const missing = Object.entries(data.sources_present || {})
        .filter(([, present]) => !present)
        .map(([app]) => app);
      if (missing.length) text += `No log yet for: ${missing.join(", ")}\n`;

      if (data.summary) {
        text += `\n${"=".repeat(60)}\nSUMMARY\n${JSON.stringify(data.summary, null, 2)}\n`;
      }
      if (data.recent && data.recent.length) {
        text += `\n${"=".repeat(60)}\nMOST RECENT (${data.recent.length})\n`;
        text += data.recent.map((r) => JSON.stringify(r)).join("\n");
      }
      return text;
    } catch (err) {
      return `Error reading performance: ${err.message}`;
    }
  },
};
