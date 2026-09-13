# External agent service protocol: ava-runtime/1

This adapter connects Ava to an agent and sandbox you operate, without installing
NemoClaw or editing Ava's runtime registry. Select:

```yaml
agent:
  runtime: service
  required: true
  service:
    url: https://agent.example.org
    timeout_s: 120
```

Supply the service's bearer token through `AVA_RUNTIME_TOKEN` or the owner-readable
file `$AVA_HOME/secrets/runtime_service_token`. Ava never generates credentials for
another service. TLS certificates are verified. Plain HTTP is allowed on loopback;
other HTTP endpoints require `agent.service.allow_http: true`. Redirects are
refused, so bearer tokens cannot follow a redirect to another origin. Responses
are streamed with an 8 MiB limit; connect timeout is five seconds and turn timeout
is bounded to 1–600 seconds.

Every request sends `X-Ava-Runtime-Version: ava-runtime/1` and, when configured,
`Authorization: Bearer <token>`. JSON responses must be objects. Non-success HTTP
responses, malformed replies and unavailable services are failures. When
`agent.required: true`, an unavailable service cannot silently become tool-less
chat. Setting it false retains Ava's existing direct-chat fallback policy.

## Required endpoints

`GET /v1/runtime` returns:

```json
{
  "protocol": "ava-runtime/1",
  "ready": true,
  "name": "my-agent",
  "model": {"id": "my-model", "provider": "my-provider"},
  "capabilities": ["turns", "tools", "sessions.discard"]
}
```

`turns` and `ready: true` are required for availability. Declare `tools` only if
the agent can actually use tools. Model and name fields are optional metadata;
absence means unknown, not an invented model name. Metadata is cached for ten
seconds, and status/model inspection never shells into another vendor's runtime.

`POST /v1/turns` receives:

```json
{
  "protocol": "ava-runtime/1",
  "text": "Summarize today's orders",
  "session_id": "instance-chat-session",
  "history": []
}
```

Return `{"reply":"The answer","tools_used":["orders_summary"]}`. A stateless
agent can use the supplied history; an agent retaining memory uses the session ID.
Use separate credentials and session namespaces for independent owners. Configure
`agent.session_prefix` when multiple Ava instances deliberately share one runtime.

## Optional capabilities

| Capability | Request | Response |
|---|---|---|
| `sessions.discard` | `POST /v1/sessions/discard`, `{session_id}` | `{ok: true}` only after erasing retained session memory |
| `sandbox.exec` | `POST /v1/exec`, `{command, timeout_s}` | `{output: "..."}` after executing inside your declared sandbox |
| `provision` | `POST /v1/provision`, `{protocol, scope, connector, desired}` | `{ok, steps, detail, scope}`; `ok` must be boolean |
| `provision.observe` | `POST /v1/observe`, `{desired}` | Evidence in the same four-scope shape used by `AgentRuntime.observe()` |

Unsupported operations are reported as unsupported and never sent to the service.
The provisioning scopes are `persona`, `policies`, `servers` and `skills`, or
`all`. `agent.service.desired` supplies a list of resource objects per scope,
using the `id`, digest and evidence fields consumed by `ava_bridge/provision.py`.
Observation returns `{"maps":{"skills":{"resource-id":"sha256"}},"sources":{"skills":"service"}}`.
Omit scopes you cannot observe. An empty map means the scope was observed and
contains no resources. An unavailable evidence source means unknown. This adapter does not infer
OpenClaw paths, copy NemoClaw policies or claim a sandbox boundary for your service.

Your runtime owns tool registration, isolation, egress and resource limits. Use
Ava's Connector SDK and consent-mediated bridge endpoints when connecting Ava
apps; do not bypass their authorization by embedding owner credentials in prompts.
The service protocol does not itself grant access to any connected app. Gateway
streaming, cron, devices and vendor-specific controls are not implied by `turns`;
implement a Python `AgentRuntime` adapter for additional control-plane capabilities.

## Runnable conformance example

```sh
AVA_EXAMPLE_TOKEN=choose-a-local-test-token python examples/runtime-service/server.py
```

Point Ava's service URL at `http://127.0.0.1:9120`, configure the same bearer token,
and ask `sum 12 8 5`. The service runs a bounded calculator tool and returns `25`.
It is stateless and supports session discard. It advertises no shell execution or
provisioning. Replace its `answer()` implementation with your own agent; this is
a protocol example, not an inference engine or a production sandbox. Ava's tests
exercise it over HTTP, including authentication failure and unsupported operations.
