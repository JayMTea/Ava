# Architecture Decision Records (ADRs)

This directory captures **significant, hard-to-reverse decisions** about Ava's
architecture. Each ADR is immutable once accepted: to change a decision, write a
new ADR that supersedes the old one, and link them.

## Why ADRs

Ava makes many reversible-but-significant choices (which engine, which boundary,
which layout tool). Capturing the *context* and *consequences*, not just the
outcome, means future-you (or a new contributor) understands *why*, not just
*what*.

## Format

We use a lightweight [MADR](https://adr.github.io/madr/)-style template. Copy
[`0000-template.md`](0000-template.md), number it sequentially, and set the status.

| Status | Meaning |
|--------|---------|
| Proposed | Under consideration |
| Accepted | Decided and in effect |
| Superseded | Replaced by a later ADR (link it) |
| Deprecated | No longer relevant |

## Index

| # | Title | Status |
|---|-------|--------|
| [0001](0001-ssot-architecture-pipeline.md) | Single-source-of-truth architecture pipeline | Accepted |
| [0002](0002-two-app-split.md) | Split Experience (`:8445`) and Admin (`:8444`) front doors | Superseded |
| [0003](0003-per-tool-egress-policies.md) | Per-tool narrow egress policies (least privilege) | Accepted |
| [0004](0004-tala-layout-engine.md) | Use TALA as the diagram layout engine | Accepted |
| [0005](0005-model-load-allocation.md) | A lease broker owns memory allocation across declared models | Accepted |
