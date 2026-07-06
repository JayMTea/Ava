// check_drift — verify Ava's architecture manifest still matches the real code.
//
// HOST-CALLBACK: asks the bridge (over the ava-knowledge /internal egress) to run
// the drift check — comparing the manifest against the installed systemd units,
// listening ports, the actual MCP tool modules, the egress policy files, and the
// rendered diagrams. Use this after any code/service change, or when the user asks
// whether the diagrams/docs are still accurate.

const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'check_drift',
  description: "Check whether Ava's architecture manifest (and therefore the "
    + "diagrams and docs) still matches the real running system — services, "
    + "ports, MCP tools, egress policies, and diagram freshness. Call this after "
    + "adding/removing a tool or service, or when the user asks if the diagrams are "
    + "up to date. Returns any drift errors (must fix) and warnings.",
  inputSchema: { type: 'object', properties: {} },
  handler: async (_args, { http, internalToken }) => {
    const r = await http.postJson(`${BRIDGE}/internal/architecture/check`, {}, {
      direct: false, timeout: 30,
      headers: { 'X-Ava-Internal-Token': internalToken },
    });
    if (r && r.error && r.ok === undefined) return `Could not run drift check: ${r.error}`;
    const errs = (r && r.errors) || [];
    const warns = (r && r.warnings) || [];
    if (r && r.ok && !warns.length) return '✅ In sync — the manifest, diagrams, and code all match.';
    const parts = [];
    if (r && r.ok) parts.push('✅ No drift errors (manifest matches the code).');
    else parts.push(`⚠️ DRIFT — ${errs.length} error(s) to fix:\n  ${errs.join('\n  ')}`);
    if (warns.length) parts.push(`Warnings:\n  ${warns.join('\n  ')}`);
    return parts.join('\n');
  },
};
