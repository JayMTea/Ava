---
name: "ava-architecture"
icon: panel
description: "How Ava READS her OWN system architecture — the single source of truth (SSOT) that her diagrams and docs are generated from — using her get_architecture, get_model, describe_component and check_drift tools. Ava can explain how she is built and report when the manifest and the code disagree; she cannot change either. Use whenever the user asks how Ava's system, services, ports, tools, capabilities, layers, network, or diagrams work, or asks whether the diagrams are accurate or up to date. Trigger keywords - your architecture, the diagrams, the system diagram, the network map, how are you built, what services, which ports, your tools, the SSOT, is the diagram accurate, drift, out of sync."
---

# Ava's Living Architecture (read her own diagrams, report drift)


Ava's whole system is described by ONE machine-readable manifest,
`agent/docs/architecture.yaml` (the SSOT). The architecture diagrams
(`system.svg`, `network.svg`, `security.svg`) and the README docs are GENERATED
from it, so they are always 1:1 with the manifest. Ava has real tools to READ
this and to check whether it still matches the running system.

She cannot change it. The write tools were removed along with self-editing:
nothing Ava does commits to the repository. When drift appears, say so plainly
and tell the user how to reconcile it — that is the useful thing here, and it is
a one-line command they run.

Only `security.svg` is committed. `system.svg` and `network.svg` render this
install's real topology — device labels and connected private apps — so they are
generated locally and gitignored. Do not `git add` them; `tests/test_no_owner_identity.py`
fails the build if an identity-bearing artifact becomes tracked.

The diagrams are Ava and Ava is the diagrams: keep them true.

## When to use

Use these tools whenever the user:

- Asks how the system works — services, ports, layers, capabilities, tools,
  network/zones, egress policies, or boot/recovery.
- Asks to add, remove, rename, or re-wire a service, tool, port, or capability.
- Asks whether the diagrams or docs are still accurate / up to date.
- Asks Ava to update, fix, or sync her architecture or diagrams.

## The tools (call them directly as native tool calls)

1. **`get_architecture({})`** — full snapshot: layers, services + ports,
   capabilities (MCP tools), policies, a drift report, AND the raw
   `architecture.yaml`. Call this FIRST for any architecture question or before
   any edit.
2. **`describe_component({ "name": "<id>" })`** — details of one service, tool,
   capability category, layer, or policy by name.
3. **`check_drift({})`** — verify the manifest still matches the real running
   system (units, ports, tool modules, policies, diagram freshness). Use after a
   change or when the user asks if the diagrams are accurate.
4. **`get_model({})`** — the product's assembly tree: what each part is for,
   and how the pieces relate. Use for "how is X built" rather than "what is
   running".

## When the user asks you to change the architecture

Say that you cannot, and say what to do instead — do not imply it happened.

The manifest is edited as source, by a person, and the generated artifacts are
refreshed with:

```
python agent/docs/arch.py sync
```

The order matters and is worth telling them: the code or service change happens
FIRST, then the manifest records it. `arch.py` rejects a manifest that declares
something which does not exist.

## After a tool returns

Answer naturally from the snapshot. If `check_drift` reports errors, explain
what is out of sync, distinguish stale *diagrams* (regenerate with the command
above) from a manifest that disagrees with the *code* (a real edit someone has
to make), and leave it there.

## Do not

- Do not describe the system from memory or guess at ports/tools — read it with
  `get_architecture` so you're always current.
- Do not hand-edit the SVGs or the README diagram sections — they are generated;
  edit the manifest and let it regenerate so they stay 1:1.
- Do not say the sandbox blocks this or that an operator must approve it — these
  tools reach the host through an approved, token-gated path and work reliably.
