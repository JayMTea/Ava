---
name: ava-platform-guide
description: Explain Ava's architecture, deployment ownership, or privacy boundaries using curated project knowledge. Use when asked how Ava's app and agent platform fit together or what the reference agent can do.
---

# Explain Ava from evidence

1. Read `knowledge/architecture/ava.md` for source ownership and execution paths.
2. Read `knowledge/policies/privacy.md` for tool permissions and data handling.
3. Answer the requested question and cite the files used. Separate source defaults
   from observed deployment facts; this agent cannot inspect the live host app.
4. For a requested action outside the granted tools, state the missing capability
   and the owner-facing command or configuration documented in those sources.
   Never describe an offline mock response as a live result.
