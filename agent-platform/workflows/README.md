# workflows/

Fixed multi-step sequences run in order by code: `agent` steps, `tool` steps and human
`approval` gates, with later steps templating earlier outputs (`{{ steps.<id>.output }}`).
Empty in the skeleton — add the project's own. Recipe: `docs/extending.md` → *Add a workflow*.

Prefer a workflow over model-driven delegation whenever the steps are known in advance: it
is cheaper, testable and auditable.

```yaml
schema_version: 1
name: triage                      # equals the file name: workflows/triage.yaml
description: Classify an incoming request, then draft a reply for approval.
inputs:
  request: {description: The incoming request text, required: true}
steps:
  - id: classify
    agent: assistant
    input: "Classify this request by urgency and topic: {{ inputs.request }}"
  - id: approve
    approval: {message: "Send a reply for: {{ steps.classify.output }}?"}
  - id: reply
    agent: assistant
    input: "Draft a reply. Classification: {{ steps.classify.output }}"
output: "{{ steps.reply.output }}"
```
