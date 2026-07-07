// read_learning_state — Ava can read her own learning state and patterns
// to understand what she's learned, what recommendations she's made, and how they were received.
//
// This provides Ava with read-only access to:
// - Code learning cycles (proposals, approvals, auto-fixes)
// - Chat learning cycles (recommendations)
// - Learning patterns and trends
// - Historical feedback on recommendations

import http from 'http';

// Bridge endpoint — same env override pattern as the sibling tools.
const BRIDGE = new URL(process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096');
const BRIDGE_HOST = BRIDGE.hostname;
const BRIDGE_PORT = Number(BRIDGE.port || 80);
const INTERNAL_TOKEN = process.env.AVA_INTERNAL_TOKEN || '';

export default {
  name: 'read_learning_state',
  description: 'Read Ava\'s learning state: code/chat cycles, recommendations, approvals, patterns, and feedback. Read-only access to understand what Ava has learned.',
  inputSchema: {
    type: 'object',
    properties: {
      state_type: {
        type: 'string',
        enum: ['code', 'chat', 'all'],
        description: 'What learning state to read: code (code cycles), chat (chat cycles), or all',
      },
      limit: {
        type: 'number',
        description: 'Maximum number of cycles to return (default 10, max 100)',
      },
      include_patterns: {
        type: 'boolean',
        description: 'Include analysis of patterns and trends (default true)',
      },
    },
    required: ['state_type'],
  },
  handler: async (args) => {
    const { state_type = 'all', limit = 10, include_patterns = true } = args;

    try {
      if (!INTERNAL_TOKEN) {
        return { error: 'AVA_INTERNAL_TOKEN not set' };
      }

      // Validate input
      if (!['code', 'chat', 'all'].includes(state_type)) {
        return { error: 'state_type must be "code", "chat", or "all"' };
      }

      // Make request to bridge /internal/learning/state endpoint
      const queryParams = new URLSearchParams({
        state_type,
        limit: Math.min(Math.max(limit || 10, 1), 100).toString(),
        patterns: include_patterns ? 'true' : 'false',
      });

      return await new Promise((resolve, reject) => {
        const options = {
          hostname: BRIDGE_HOST,
          port: BRIDGE_PORT,
          path: `/internal/learning/state?${queryParams}`,
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
      return { error: `Failed to read learning state: ${error.message}` };
    }
  },
};
