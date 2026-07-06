// read_document — read the text of a file the user uploaded to the phone bridge.
//
// HOST-CALLBACK: the bridge (host) already extracted the text (pdftotext /
// LibreOffice / OCR) at upload time; this fetches it over the `ava-knowledge`
// egress policy, authenticated with the shared internal token. Use
// list_documents first to get the file's id.

const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'read_document',
  description: "Read the text contents of a file the user uploaded (document, PDF, "
    + "spreadsheet, or an image they shared). Call this whenever the user asks you to "
    + "summarize, answer questions about, quote, or use the contents of a file "
    + "they attached. Pass the file's id from list_documents. Returns the extracted "
    + "text so you can work with it.",
  inputSchema: {
    type: 'object',
    properties: {
      file_id: { type: 'string', description: "The uploaded file's id (from list_documents)." },
      max_chars: { type: 'integer', description: 'Optional cap on how much text to return.' },
    },
    required: ['file_id'],
  },
  handler: async (args, { http, internalToken }) => {
    const fileId = args && args.file_id && String(args.file_id).trim();
    if (!fileId) throw new Error('file_id is required (get it from list_documents)');
    const body = { file_id: fileId };
    if (args && args.max_chars) body.max_chars = parseInt(args.max_chars, 10) || 0;
    const r = await http.postJson(`${BRIDGE}/internal/extract`, body, {
      direct: false, timeout: 60,
      headers: { 'X-Ava-Internal-Token': internalToken },
    });
    const text = ((r && r.text) || '').trim();
    const name = (r && r.filename) || fileId;
    if (!text) return `"${name}" has no readable text to return.`;
    return `Contents of "${name}":\n\n${text}`;
  },
};
