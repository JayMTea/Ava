// device_events — read recent events that the user's own device/sensor apps have
// pushed to Ava (motion, thresholds, readings, etc.).
//
// HOST-CALLBACK: a connector app with an `ingest:` block POSTs events to the
// bridge (POST /api/connectors/<id>/events); the bridge stores them
// (ava_bridge/devices.py). This tool reads them back over the `ava-knowledge`
// egress policy, authenticated with the scoped internal token — so Ava can answer
// "did anything happen in the greenhouse?" or react to what a device reported.

const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'device_events',
  description: "Read recent events pushed to Ava by the user's connected device or "
    + "sensor apps (e.g. motion detected, a temperature reading, a threshold "
    + "crossed). Call this when the user asks what their devices/sensors have "
    + "reported, whether anything happened, or the latest reading from a device. "
    + "Optionally pass a connector id to look at just one app. Returns the events "
    + "newest-first with their name, value/unit, message, severity and time.",
  inputSchema: {
    type: 'object',
    properties: {
      connector: { type: 'string', description: "Optional connector id to filter to a single device app (omit for all)." },
      limit: { type: 'integer', description: 'Max events to return (default 50, max 200).' },
    },
    additionalProperties: false,
  },
  handler: async (args, { http, internalToken }) => {
    const params = new URLSearchParams();
    if (args && args.connector) params.set('connector', String(args.connector).trim());
    if (args && args.limit) params.set('limit', String(parseInt(args.limit, 10) || 50));
    const qs = params.toString();
    const r = await http.getJson(`${BRIDGE}/internal/devices/events${qs ? '?' + qs : ''}`, {
      direct: false, timeout: 20,
      headers: { 'X-Ava-Internal-Token': internalToken },
    });
    const events = (r && r.events) || [];
    if (!events.length) return 'No device events have been reported yet.';
    const lines = events.map((e) => {
      const when = new Date((e.ts || 0) * 1000).toISOString().replace('T', ' ').slice(0, 19);
      const val = e.value !== undefined ? ` = ${e.value}${e.unit ? ' ' + e.unit : ''}` : '';
      const msg = e.message ? ` — ${e.message}` : '';
      const sev = e.severity ? ` [${e.severity}]` : '';
      return `${when}  ${e.cid}/${e.name}${val}${msg}${sev}`;
    });
    return `Recent device events (newest first):\n${lines.join('\n')}`;
  },
};
