// sync_diagrams — regenerate Ava's diagrams + docs from the manifest and commit.
//
// HOST-CALLBACK: asks the bridge (over the ava-knowledge /internal egress) to run
// the generator — re-render system.d2/.svg + network.d2/.svg and the README
// services table from architecture.yaml, then auto-commit if anything changed.
// Use this if the diagrams look stale but the manifest is already correct.

const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'sync_diagrams',
  description: "Regenerate Ava's architecture diagrams (SVGs) and the README "
    + "services table from the manifest, and auto-commit the result. Use when the "
    + "diagrams are stale but architecture.yaml is already correct. To CHANGE the "
    + "architecture, edit the manifest with update_architecture instead (it syncs "
    + "automatically). Returns what was regenerated and whether it was committed.",
  inputSchema: {
    type: 'object',
    properties: {
      message: { type: 'string', description: 'Optional commit message.' },
      commit: { type: 'boolean', description: 'Commit the result (default true).' },
    },
  },
  handler: async (args, { http, internalToken }) => {
    const body = {};
    if (args && args.message) body.message = String(args.message);
    if (args && args.commit === false) body.commit = false;
    const r = await http.postJson(`${BRIDGE}/internal/architecture/sync`, body, {
      direct: false, timeout: 120,
      headers: { 'X-Ava-Internal-Token': internalToken },
    });
    const d = (r && r.drift) || {};
    const regen = (r && r.regenerated) || [];
    const drift = d.ok === false
      ? `⚠️ drift remains: ${(d.errors || []).join('; ')}`
      : '✅ in sync';
    return `Regenerated: ${regen.join(', ') || '(no changes)'}\n`
      + `Committed: ${r && r.committed ? 'yes' : 'no (nothing changed)'}\n${drift}`;
  },
};
