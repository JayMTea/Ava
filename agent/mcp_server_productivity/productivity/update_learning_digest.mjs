// update_learning_digest — Ava can update the learning digest generation scripts
// to add better descriptions, rationales, and formatting for her recommendations.
//
// This allows Ava to self-improve the digest output without needing host access.
// Changes are persisted to the scripts and take effect on the next scheduled run
// (daily at 4am PST via systemd timer) or manual execution.

import http from 'http';

const BRIDGE_HOST = 'host.openshell.internal';
const BRIDGE_PORT = 8096;
const INTERNAL_TOKEN = process.env.AVA_INTERNAL_TOKEN || '';

export default {
  name: 'update_learning_digest',
  description: 'Update the learning digest generation scripts (ava_learning_digest.py, ava_learning_weekly.py) to improve recommendation descriptions and rationales.',
  inputSchema: {
    type: 'object',
    properties: {
      script_type: {
        type: 'string',
        enum: ['daily', 'weekly', 'both'],
        description: 'Which digest script(s) to update: daily (ava_learning_digest.py), weekly (ava_learning_weekly.py), or both',
      },
      updates: {
        type: 'object',
        description: 'Key updates to apply (e.g., { improve_rationale: true, add_confidence: true })',
        properties: {
          improve_rationale: {
            type: 'boolean',
            description: 'Add detailed rationales explaining WHY each recommendation was made',
          },
          add_confidence: {
            type: 'boolean',
            description: 'Include confidence scores (0-100%) for each recommendation',
          },
          add_context: {
            type: 'boolean',
            description: 'Include relevant context snippets and metrics',
          },
          add_impact: {
            type: 'boolean',
            description: 'Include expected impact/outcome of following recommendations',
          },
          improve_formatting: {
            type: 'boolean',
            description: 'Enhance HTML formatting and visual hierarchy',
          },
        },
      },
      content: {
        type: 'string',
        description: 'Custom Python code snippet or full script content to merge/replace sections',
      },
    },
    required: ['script_type', 'updates'],
  },
  handler: async (args) => {
    const { script_type, updates, content } = args;

    try {
      if (!INTERNAL_TOKEN) {
        return { error: 'AVA_INTERNAL_TOKEN not set' };
      }

      // Build request body
      const body = JSON.stringify({
        script_type,
        updates,
        content: content || null,
      });

      // Make request to bridge /internal/learning/update endpoint
      return await new Promise((resolve, reject) => {
        const options = {
          hostname: BRIDGE_HOST,
          port: BRIDGE_PORT,
          path: '/internal/learning/update',
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(body),
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

        req.write(body);
        req.end();
      });
    } catch (error) {
      return { error: `Update failed: ${error.message}` };
    }
  },
};
