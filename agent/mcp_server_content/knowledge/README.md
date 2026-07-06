# knowledge/ — facts, lookup & utilities

General "look it up / figure it out" tools Ava uses to answer questions.

**Live tools:**
- `list_documents` — list the files the user uploaded to the phone bridge (id, name, type).
- `read_document` — return the extracted text of one uploaded file (PDF / Office /
  text / OCR'd image). Host-callback: the sandbox can't see the host's upload dir
  or extraction binaries, so both tools call the bridge's token-gated `/internal/*`
  endpoints via host.openshell.internal under the `ava-knowledge` egress policy.
  Skill: `agent/skills/ava-knowledge/`.

**Planned ideas:**
- `web_search` — general search (pick a privacy-respecting backend; needs a policy).
- `wikipedia` — summary lookup (Wikipedia REST API, no key).
- `calculate` — math / unit / currency conversion (local; currency needs a rate source).
- `translate` — text translation (local model or a no-key endpoint).
- `define` — dictionary lookup.

Network tools need a least-privilege egress policy in `agent/policies/`. Add one:
`agent/new-tool.sh wikipedia --category knowledge --with-policy --with-skill`

