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
        description: "How many most-recent records to summarise verbatim (1-500, default 8). Keep small — each record is large and a big dump makes the reply slow."
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
      // Default to a SMALL verbatim window. Each raw record is ~600 bytes; the
      // old default (50) returned ~32 KB / ~8k tokens, which the local model
      // couldn't summarise before the turn timed out. Summary + a few recent
      // rows is what actually answers "how fast am I?".
      const a = { ...(args || {}) };
      if (a.limit == null) a.limit = 8;
      a.limit = Math.min(Math.max(parseInt(a.limit, 10) || 8, 1), 500);

      // Reach the bridge through the sandbox http helper (curl-based, proxy/CA
      // aware) — NOT Node's built-in fetch, which the deny-by-default network
      // guard blocks/hangs (that was the real cause of the turn timing out).
      const data = await ctx.http.postJson(`${BRIDGE}/internal/perf`, a, {
        direct: false, timeout: 25,
        headers: { "X-Ava-Internal-Token": ctx.internalToken || "" },
      });
      if (!data || !data.ok) {
        return `Error reading performance: ${(data && data.error) || "bridge unreachable"}`;
      }

      let text = `Generation performance — ${data.apps.join(", ")}\n`;
      text += `Total records: ${data.total}\n`;
      const missing = Object.entries(data.sources_present || {})
        .filter(([, present]) => !present)
        .map(([app]) => app);
      if (missing.length) text += `No log yet for: ${missing.join(", ")}\n`;

      if (data.summary) {
        text += `\n${"=".repeat(60)}\nSUMMARY\n${JSON.stringify(data.summary, null, 2)}\n`;
      }
      if (data.recent && data.recent.length) {
        // Compact one line per record (key fields only) — never a raw JSON dump,
        // so the payload stays small even if a large limit is requested.
        const rows = data.recent.slice(0, 25).map((r) => {
          const when = (r.iso || r.ts || "").toString().slice(0, 19);
          const who = r.served_label || r.served_by || r.category || "?";
          const tps = r.tokens_per_sec != null ? `${r.tokens_per_sec} tok/s` : "";
          const ttft = r.ttft_ms != null ? `ttft ${r.ttft_ms}ms` : "";
          const out = r.completion_tokens != null ? `${r.completion_tokens} out` : "";
          const secs = r.gen_seconds != null ? `${r.gen_seconds}s` : "";
          return `${when}  ${r.category || "llm"}  ${who}  ${[tps, ttft, out, secs].filter(Boolean).join("  ")}`.trimEnd();
        });
        text += `\n${"=".repeat(60)}\nMOST RECENT (${rows.length}${data.recent.length > rows.length ? ` of ${data.recent.length}` : ""})\n`;
        text += rows.join("\n");
      }
      return text;
    } catch (err) {
      return `Error reading performance: ${err.message}`;
    }
  },
};
