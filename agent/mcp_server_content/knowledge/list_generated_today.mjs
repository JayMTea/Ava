// list_generated_today — show generated media outputs for flexible time ranges.
//
// This lets Ava answer: "what did we generate Monday / last week / 2026-07-01?"
// with concrete items and /media URLs that can be pulled into chat context.

const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'list_generated_today',
  description: 'List generated media content by date phrase or explicit range. Supports today, yesterday, monday..sunday, last week, this week, YYYY-MM-DD, or from/to dates.',
  inputSchema: {
    type: 'object',
    properties: {
      when: {
        type: 'string',
        description: 'Time phrase or specific date, e.g. today, monday, last week, 2026-07-02',
      },
      from: {
        type: 'string',
        description: 'Optional start date YYYY-MM-DD (used with or without `to`).',
      },
      to: {
        type: 'string',
        description: 'Optional end date YYYY-MM-DD (inclusive day).',
      },
      persona: {
        type: 'string',
        description: 'Optional persona key/name filter, e.g. april',
      },
      limit: {
        type: 'integer',
        description: 'Maximum number of items to return (default 40, max 120).',
      },
    },
    required: [],
  },
  handler: async (args, { http, internalToken }) => {
    const persona = (args && args.persona ? String(args.persona).trim() : '');
    const when = (args && args.when ? String(args.when).trim() : 'today');
    const from = (args && args.from ? String(args.from).trim() : '');
    const to = (args && args.to ? String(args.to).trim() : '');
    const lim = Math.max(1, Math.min(parseInt(args && args.limit, 10) || 40, 120));
    const q = new URLSearchParams({ limit: String(lim) });
    if (when) q.set('when', when);
    if (from) q.set('from', from);
    if (to) q.set('to', to);
    if (persona) q.set('persona', persona);

    const r = await http.getJson(`${BRIDGE}/internal/studio/content?${q.toString()}`, {
      direct: false,
      timeout: 20,
      headers: { 'X-Ava-Internal-Token': internalToken },
    });
    if (r && r.error) throw new Error(r.error);

    const items = (r && r.items) || [];
    if (!items.length) {
      return `No generated content found for ${r?.when || when || 'that range'}.`;
    }

    const lines = items.map((it, i) => {
      const k = it.kind || 'item';
      const p = it.persona || 'unknown persona';
      const t = it.time || '';
      const seed = (it.seed !== null && it.seed !== undefined) ? ` seed ${it.seed}` : '';
      const u = it.url || '';
      return `${i + 1}. [${k}] ${p} ${t}${seed}\n   ${u}`;
    });

    return `Generated for ${r.when || when} (${r.start} .. ${r.end_exclusive}) — ${items.length} item(s):\n${lines.join('\n')}`;
  },
};
