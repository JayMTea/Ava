// update_architecture — apply an edit to Ava's architecture manifest (SSOT).
//
// HOST-CALLBACK: sends a full new architecture.yaml to the bridge (over the
// ava-knowledge /internal egress). arch.py validates it, regenerates the
// diagrams + README tables, runs a drift gate (reverts if the manifest no longer
// matches the real code), and auto-commits on success. This is how Ava keeps the
// diagrams and docs 1:1 with the system.

const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'update_architecture',
  description: "Apply an edit to Ava's architecture — submit the COMPLETE new "
    + "architecture.yaml (get the current one from get_architecture, edit it, then "
    + "send the whole document back). The diagrams and README are regenerated and "
    + "auto-committed automatically, so they stay 1:1 with the system. The edit is "
    + "REJECTED and reverted if it doesn't match the real running code (e.g. you "
    + "declared a tool/service that doesn't exist yet) — change the code first, "
    + "then record it here. Returns success + the drift report, or the error.",
  inputSchema: {
    type: 'object',
    properties: {
      yaml: { type: 'string', description: 'The full new architecture.yaml document (not a diff).' },
      message: { type: 'string', description: 'Optional commit message describing the change.' },
      commit: { type: 'boolean', description: 'Auto-commit on success (default true).' },
    },
    required: ['yaml'],
  },
  handler: async (args, { http, internalToken }) => {
    const doc = args && args.yaml;
    if (!doc || !String(doc).trim()) {
      throw new Error('yaml is required — the full new architecture.yaml (get it from get_architecture first)');
    }
    const body = { yaml: String(doc) };
    if (args.message) body.message = String(args.message);
    if (args.commit === false) body.commit = false;
    const r = await http.postJson(`${BRIDGE}/internal/architecture/update`, body, {
      direct: false, timeout: 180,
      headers: { 'X-Ava-Internal-Token': internalToken },
    });
    if (!r || !r.ok) {
      const d = (r && r.drift) || {};
      const extra = (d.errors || []).length ? `\n  ${d.errors.join('\n  ')}` : '';
      return `Update rejected: ${(r && r.error) || 'unknown error'}${extra}`;
    }
    return `Architecture updated, diagrams + docs regenerated.\n`
      + `Committed: ${r.committed ? 'yes' : 'no'}\n`
      + `${r.drift && r.drift.ok ? 'In sync.' : 'Note: drift report attached.'}`;
  },
};
