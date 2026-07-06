// list_documents — see which files the user has uploaded to the phone bridge.
//
// HOST-CALLBACK: Ava runs in the sandbox and can't see the host's upload dir, so
// this asks the phone bridge (host) over the `ava-knowledge` egress policy. The
// bridge holds the uploaded-file records; this returns their ids/names/types so
// Ava can then read one with read_document. Authenticated with the shared
// internal token handed to the server at launch.

const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {
  name: 'list_documents',
  description: "List the files (documents, PDFs, spreadsheets, images) the user has "
    + "uploaded to the chat so you can read them. Call this when the user refers to a "
    + "file, document, PDF, or attachment they shared and you need to know what's "
    + "available or get its id. Returns each upload's id, filename, type, and "
    + "whether text was extracted. Then call read_document with the id.",
  inputSchema: { type: 'object', properties: {} },
  handler: async (_args, { http, internalToken }) => {
    const r = await http.getJson(`${BRIDGE}/internal/documents`, {
      direct: false, timeout: 15,
      headers: { 'X-Ava-Internal-Token': internalToken },
    });
    const docs = (r && r.documents) || [];
    if (!docs.length) return 'The user has not uploaded any files in this conversation.';
    const lines = docs.map(d =>
      `- ${d.filename} (${d.kind}, id=${d.id}`
      + `${d.has_text ? `, ${d.chars} chars` : ', no extractable text'})`);
    return `Uploaded files:\n${lines.join('\n')}`;
  },
};
