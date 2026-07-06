// HOST-CALLBACK: reaches the bridge through the guard proxy via the
// host.openshell.internal alias (NOT the Tailscale IP + Node fetch, which the
// deny-by-default network guard blocks). Uses the curl-based http.postJson
// helper so the proxy + internal token are honored, matching the working
// content/persona tools. The bridge runs Claude host-side, so the timeout is
// generous.
const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'code_change_request',
  description: "Change or fix source code, Ava. Hands the task to Claude (uses the owner's ANTHROPIC_API_KEY) which edits the repo directly. Defaults to your OWN repo (project 'ava'): safe files (UI, tools, docs, most bridge modules) are applied + committed automatically, protected files (auth, config, egress policy, deploy scripts) are parked in the user's approval bucket on the Learning page. If other projects are configured on this install, they can be targeted by id — approval-only, on a review branch (never the mainline), and gated by a compile/test check. The valid set is enforced server-side (config.PROJECTS). Secrets and biometrics are never touched. Use this to implement changes the user asks for AND to fix bugs/errors you notice.",
  inputSchema: {
    type: "object",
    properties: {
      request: {
        type: "string",
        description: "Clear description of the code change or fix. Say what to change, where (file/function), and why."
      },
      project: {
        type: "string",
        description: "Which repo to edit. 'ava' (default) = your own source. Additional connected projects (only if configured on this install, approval-only + branch-based + test-gated) may be targeted by id. The valid set is enforced server-side (config.PROJECTS); only name a non-'ava' project when the change clearly belongs to it."
      },
      context: {
        type: "string",
        description: "Optional extra context: the exact error message/traceback you hit, expected behavior, or constraints."
      },
      files: {
        type: "array",
        items: { type: "string" },
        description: "Optional repo-relative files to focus on (e.g. 'ava_bridge/web/index.html', 'agent/mcp_server_content/creative/image.mjs')."
      }
    },
    required: ["request"],
    additionalProperties: false
  },

  async handler(args, { http, internalToken }) {
    const { request, context, files, project } = args;

    let data;
    try {
      data = await http.postJson(`${BRIDGE}/internal/code-change`,
        { request, context, files, project: project || 'ava' },
        { direct: false, timeout: 220,
          headers: { 'X-Ava-Internal-Token': internalToken || '' } });
    } catch (err) {
      return `❌ Error reaching the code-change handler: ${err.message}`;
    }
    if (data && data.error) {
      return `❌ Code change failed: ${data.error}`;
    }

    const status = data.status || "completed";
    const applied = data.files_changed || [];
    const pending = data.pending_approval || [];
    const errors = data.errors || [];

    if (status === "no_changes") {
      return `ℹ️ No code changes were needed.\n\n${data.summary || ""}`.trim();
    }

    const head = {
      completed: "✅ Code changes applied",
      partial: "✅ Some changes applied · ⏳ some need the user's approval",
      pending_approval: "⏳ Change staged — waiting for the user's approval",
      blocked: "🚫 Change blocked by access policy",
      error: "❌ Code change error",
    }[status] || "✅ Code changes applied";

    let out = head + "\n\n";
    if (data.summary) out += `📝 ${data.summary}\n\n`;

    if (applied.length) {
      out += `📂 **Applied** (${applied.length})\n`;
      for (const f of applied) out += `  • ${f.path}${f.description ? " — " + f.description : ""}\n`;
      if (data.git_commit) out += `  🔗 commit ${data.git_commit}\n`;
      out += "\n";
    }

    if (pending.length) {
      out += `⏳ **Awaiting the user's approval** (${pending.length}) — protected files\n`;
      for (const f of pending) out += `  • ${f.path}${f.description ? " — " + f.description : ""}\n`;
      out += `  → Review & approve on the Learning page.\n\n`;
    }

    if (errors.length) {
      out += `⚠️ **Notes**\n`;
      for (const e of errors) out += `  • ${e}\n`;
    }

    return out.trim();
  }
};
