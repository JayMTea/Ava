// read_code_turns — Ava can read her own code editing turns and diffs
// to understand patterns in code changes, what gets approved, and improvement areas.
//
// This provides Ava with read-only access to:
// - Code editing turns (proposals, changes, diffs)
// - Approval status and feedback
// - Files modified and impact assessment
// - Reasoning and decision history

import http from 'http';

// Bridge endpoint — same env override pattern as the sibling tools.
const BRIDGE = new URL(process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096');
const BRIDGE_HOST = BRIDGE.hostname;
const BRIDGE_PORT = Number(BRIDGE.port || 80);
const INTERNAL_TOKEN = process.env.AVA_INTERNAL_TOKEN || '';

export default {
  name: 'read_code_turns',
  description: 'Read Ava\'s code editing history: turns, proposals, diffs, approvals, and feedback. Read-only access to understand code patterns.',
  inputSchema: {
    type: 'object',
    properties: {
      cycle_id: {
        type: 'string',
        description: 'Specific code cycle ID to read (optional, reads latest if omitted)',
      },
      status: {
        type: 'string',
        enum: ['all', 'approved', 'denied', 'pending'],
        description: 'Filter by approval status (default: all)',
      },
      include_diffs: {
        type: 'boolean',
        description: 'Include full diffs for each proposal (default true)',
      },
      include_reasoning: {
        type: 'boolean',
        description: 'Include reasoning/chain-of-thought for decisions (default true)',
      },
      limit: {
        type: 'number',
        description: 'Maximum number of turns to return (default 10, max 100)',
      },
    },
  },
  handler: async (args) => {
    const {
      cycle_id = null,
      status = 'all',
      include_diffs = true,
      include_reasoning = true,
      limit = 10,
    } = args;

    try {
      if (!INTERNAL_TOKEN) {
        return { error: 'AVA_INTERNAL_TOKEN not set' };
      }

      // Validate input
      if (!['all', 'approved', 'denied', 'pending'].includes(status)) {
        return { error: 'status must be "all", "approved", "denied", or "pending"' };
      }

      // Make request to bridge /internal/learning/code-turns endpoint
      const queryParams = new URLSearchParams({
        status,
        diffs: include_diffs ? 'true' : 'false',
        reasoning: include_reasoning ? 'true' : 'false',
        limit: Math.min(Math.max(limit || 10, 1), 100).toString(),
        ...(cycle_id && { cycle_id }),
      });

      return await new Promise((resolve, reject) => {
        const options = {
          hostname: BRIDGE_HOST,
          port: BRIDGE_PORT,
          path: `/internal/learning/code-turns?${queryParams}`,
          method: 'GET',
          headers: {
            'Authorization': `Bearer ${INTERNAL_TOKEN}`,
            'X-Ava-Internal-Token': INTERNAL_TOKEN,
          },
        };

        const req = http.request(options, (res) => {
          let data = '';
          res.on('data', chunk => data += chunk);
          res.on('end', () => {
            try {
              const json = JSON.parse(data);
              resolve(json);
            } catch (e) {
              resolve({ error: 'Invalid JSON response', raw: data });
            }
          });
        });

        req.on('error', err => {
          resolve({ error: `Request failed: ${err.message}` });
        });

        req.end();
      });
    } catch (error) {
      return { error: `Failed to read code turns: ${error.message}` };
    }
  },
};
