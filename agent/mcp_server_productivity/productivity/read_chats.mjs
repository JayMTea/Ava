// read_chats — Ava can read her own chat history
// to reflect on conversations, identify patterns, and improve future responses.
//
// This provides Ava with read-only access to:
// - Chat list with titles and metadata
// - Full message history for a specific chat
// - Message roles (user, assistant) and timestamps
// - Attachments and media references

import http from 'http';

// Bridge endpoint — same env override pattern as the sibling tools.
const BRIDGE = new URL(process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096');
const BRIDGE_HOST = BRIDGE.hostname;
const BRIDGE_PORT = Number(BRIDGE.port || 80);
const INTERNAL_TOKEN = process.env.AVA_INTERNAL_TOKEN || '';

export default {
  name: 'read_chats',
  description: 'Read Ava\'s chat history: list chats, read messages from a specific chat, examine conversation patterns. Read-only access.',
  inputSchema: {
    type: 'object',
    properties: {
      action: {
        type: 'string',
        enum: ['list', 'read'],
        description: 'Action: list (get all chats) or read (get messages from a specific chat)',
      },
      chat_id: {
        type: 'string',
        description: 'Chat ID (required when action=read)',
      },
      limit: {
        type: 'number',
        description: 'Maximum number of messages to return (default 50, max 200)',
      },
      include_metadata: {
        type: 'boolean',
        description: 'Include message metadata like timestamps and attachments (default true)',
      },
    },
    required: ['action'],
  },
  handler: async (args) => {
    const { action = 'list', chat_id = null, limit = 50, include_metadata = true } = args;

    try {
      if (!INTERNAL_TOKEN) {
        return { error: 'AVA_INTERNAL_TOKEN not set' };
      }

      // Validate input
      if (!['list', 'read'].includes(action)) {
        return { error: 'action must be "list" or "read"' };
      }

      if (action === 'read' && !chat_id) {
        return { error: 'chat_id required when action="read"' };
      }

      // Make request to bridge /internal/learning/chats endpoint
      const queryParams = new URLSearchParams({
        action,
        ...(chat_id && { chat_id }),
        limit: Math.min(Math.max(limit || 50, 1), 200).toString(),
        metadata: include_metadata ? 'true' : 'false',
      });

      return await new Promise((resolve, reject) => {
        const options = {
          hostname: BRIDGE_HOST,
          port: BRIDGE_PORT,
          path: `/internal/learning/chats?${queryParams}`,
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
      return { error: `Failed to read chats: ${error.message}` };
    }
  },
};
