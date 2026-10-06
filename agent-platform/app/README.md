# app/ — product surfaces

Everything a user or another system talks to: the CLI that ships with the template
(`app/cli.py`), and whatever your project adds — an HTTP API, a chat UI, a Slack bot, a
scheduled job, an A2A server.

## The one rule

App code talks to the platform through `runtime.Platform` only:

```python
from runtime import Platform

platform = Platform.load(".", approver=MyUiApprover())
result = platform.run(user_message)          # or agent="<name>"
```

App code never calls a model SDK, never runs a tool directly, and never reads `state/`
files. Those paths bypass policy enforcement, tracing and approvals.

## When you add a surface

- **Approvals:** pass an `Approver` that asks the human through your UI. Without one,
  `policies/approvals.yaml` `non_interactive: deny` applies.
- **Identity:** map the caller to what they may do *before* calling `run`. The platform
  enforces agent permissions; it does not know who your users are.
- **Sessions:** create one `Platform` per conversation (it owns a session id for traces and
  session memory) or pass `session_id=` explicitly.
- **A2A:** an inbound A2A request is an untrusted caller. Route it via
  `protocols/a2a/routes.yaml` to a workflow or agent, with no approver attached.
