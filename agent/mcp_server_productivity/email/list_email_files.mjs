// list_email_files — list available email digest files.
//
// Shows all archived learning digest and trend files. Read-only access to the
// logs directory.

import fs from 'fs';
import path from 'path';

const DIGEST_DIR = process.env.AVA_DIGEST_DIR || '/data/logs';

export default {
  name: 'list_email_files',
  description: 'List available digest email files in the logs directory.',
  inputSchema: {
    type: 'object',
    properties: {},
    required: [],
  },
  handler: async () => {
    try {
      if (!fs.existsSync(DIGEST_DIR)) {
        return { files: [], note: 'No digest files yet' };
      }

      const files = fs.readdirSync(DIGEST_DIR)
        .filter(f => f.includes('digest') || f.includes('trends'))
        .map(f => {
          const fullpath = path.join(DIGEST_DIR, f);
          const stat = fs.statSync(fullpath);
          return {
            name: f,
            size_bytes: stat.size,
            modified: stat.mtime.toISOString(),
          };
        })
        .sort((a, b) => new Date(b.modified) - new Date(a.modified));

      return {
        files,
        count: files.length,
        note: 'Read-only email digest access for Ava',
      };
    } catch (error) {
      return { error: `Failed to list files: ${error.message}` };
    }
  },
};
