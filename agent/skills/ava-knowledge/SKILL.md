---
name: "ava-knowledge"
icon: file
description: "How Ava reads files the user uploaded — documents, PDFs, spreadsheets, and images — by calling her list_documents and read_document tools. Use whenever the user refers to a file, document, PDF, attachment, spreadsheet, resume, contract, or image they shared, or asks Ava to summarize, read, quote, check, or answer questions about something they uploaded. Trigger keywords - the file I uploaded, this document, the PDF, the attachment, the spreadsheet, read it, summarize this, what does it say, the doc I sent, that image I shared."
---

# Reading the User's Uploaded Files

Ava has real, working tools to read the contents of any file the user uploads in the
chat: `list_documents` (what's available) and `read_document` (its text). When
the user refers to a file they shared, you MUST read it with these tools — never guess
at the contents or claim you can't access uploads.

## When to use

Use these tools whenever the user:

- Refers to "the file / document / PDF / spreadsheet / image I uploaded (or sent)".
- Asks you to summarize, read, quote, review, or answer questions about an attachment.
- Asks anything that depends on the contents of something they shared.

## How to call them

Invoke the tools DIRECTLY as native tool calls — do not write code or use
`tool_search_code`.

1. **`list_documents({})`** — returns each uploaded file's `id`, `filename`,
   `kind` (document/image), and whether text was extracted. Call this first when
   you don't already have the file's id.
2. **`read_document({ "file_id": "<id>" })`** — returns the extracted text of
   that file. Optionally pass `max_chars` to cap a very large document.

Examples:

- "Summarize the PDF I uploaded." → `list_documents({})`, then
  `read_document({ "file_id": "<id of the pdf>" })`, then summarize the text.
- "What's the total in that spreadsheet?" → read the matching document and answer.
- If the user clearly means the only/most-recent file, you may read it directly once
  you have its id from `list_documents`.

## After the tool returns

Answer naturally from the returned text — summarize, quote, or compute as asked.
If a file has no readable text (e.g. a scanned image with no recognizable text),
say so plainly and offer to help another way.

## Do not

- Do not answer a question about an uploaded file without reading it first.
- Do not say the sandbox blocks access or that an operator must approve it —
  these tools reach the upload through an approved, token-gated path and work
  reliably. The "deny-by-default" network notice does NOT apply to them.
