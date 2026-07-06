// get_emails — read recent emails from Outlook IMAP (read-only).
//
// Host-callback to the bridge's /internal/emails endpoint, which handles
// Python IMAP connection + parsing. This MCP tool just proxies the call.
// Falls back to local digest files if IMAP is unavailable.
// Read-only access only.

const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'get_emails',
  description: 'Read recent emails from your Outlook inbox. Fetches the latest messages with subject, from, date, and text snippet. Falls back to learning digest if IMAP unavailable. Read-only access only.',
  inputSchema: {
    type: 'object',
    properties: {
      count: {
        type: 'number',
        description: 'Number of recent emails to fetch (default 10, max 50)',
        default: 10,
      },
      unread_only: {
        type: 'boolean',
        description: 'Fetch only unread emails (default false)',
        default: false,
      },
      mailbox: {
        type: 'string',
        description: 'Mailbox to fetch from (default "INBOX")',
        default: 'INBOX',
      },
    },
    required: [],
  },
  handler: async (args, { http, internalToken }) => {
    const { count = 10, unread_only = false, mailbox = 'INBOX' } = args;

    if (count < 1 || count > 50) {
      return { error: 'count must be between 1 and 50' };
    }

    try {
      const r = await http.getJson(
        `${BRIDGE}/internal/emails?count=${count}&unread_only=${unread_only}&mailbox=${encodeURIComponent(mailbox)}`,
        {
          direct: false,
          timeout: 30,
          headers: { 'X-Ava-Internal-Token': internalToken },
        }
      );

      if (!r || r.error) {
        return { error: r?.error || 'Failed to fetch emails from bridge' };
      }

      return {
        emails: r.emails || [],
        count: r.count || 0,
        mailbox,
        note: 'Read-only access. Fetched via Outlook IMAP.',
      };
    } catch (error) {
      return { error: `Email fetch failed: ${error.message}` };
    }
  },
};
