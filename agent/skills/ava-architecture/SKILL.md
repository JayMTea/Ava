---
name: "ava-architecture"
description: "How Ava reads and updates her OWN system architecture — the single source of truth (SSOT) that her diagrams and docs are generated from — using her get_architecture, describe_component, check_drift, sync_diagrams, and update_architecture tools. Use whenever the user asks how Ava's system, services, ports, tools, capabilities, layers, network, or diagrams work; asks to change/add/remove a service or tool; asks whether the diagrams are accurate or up to date; or asks Ava to update her architecture. Trigger keywords - your architecture, the diagrams, the system diagram, the network map, how are you built, what services, which ports, your tools, the SSOT, update the diagram, is the diagram accurate, drift, keep the docs in sync."
---

# Ava's Living Architecture (read + update her own diagrams)

Ava's whole system is described by ONE machine-readable manifest,
`agent/docs/architecture.yaml` (the SSOT). The architecture diagrams
(`system.svg`, `network.svg`) and the README docs are GENERATED from it, so they
are always 1:1 with the manifest. Ava has real tools to read this and to change
it — when she edits the manifest, the diagrams and docs regenerate and commit
automatically.

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
4. **`sync_diagrams({})`** — regenerate the diagrams + README tables from the
   manifest and auto-commit. Use when the diagrams are stale but the manifest is
   already correct.
5. **`update_architecture({ "yaml": "<full new architecture.yaml>", "message": "<why>" })`**
   — apply a change. Submit the COMPLETE edited manifest (not a diff). The
   diagrams + docs regenerate and auto-commit.

## How to change the architecture

1. `get_architecture({})` and read the raw YAML.
2. Edit the YAML in place — change the one thing the user asked for, keep everything
   else identical and valid.
3. Send the WHOLE document back via `update_architecture`.

The edit is **rejected and reverted** if the manifest no longer matches reality —
for example if you declare a tool or service that doesn't actually exist yet. So
the order is: the code/service change happens FIRST, then you record it in the
manifest. If the user only wants a doc/diagram fix, editing the manifest is the change.

## After a tool returns

Answer naturally from the snapshot. After an `update_architecture` or
`sync_diagrams`, tell the user what changed, that the diagrams + docs were regenerated,
and whether it was committed. If `check_drift` reports errors, explain what's out
of sync and offer to fix it.

## Do not

- Do not describe the system from memory or guess at ports/tools — read it with
  `get_architecture` so you're always current.
- Do not hand-edit the SVGs or the README diagram sections — they are generated;
  edit the manifest and let it regenerate so they stay 1:1.
- Do not say the sandbox blocks this or that an operator must approve it — these
  tools reach the host through an approved, token-gated path and work reliably.
