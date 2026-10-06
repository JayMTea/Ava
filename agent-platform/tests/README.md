# tests/

Two kinds of tests, on purpose:

| Kind | Runs against | Changes when |
|---|---|---|
| **Runtime tests** — agent loop, routing, providers, executor, workflows, memory, tools | `tests/fixtures/platform/` (fixture agents `lead` and `helper`) | The runtime's behaviour changes |
| **Project tests** — config consistency, coding-agent skill parity, `integration/test_project_guarantees.py` | This project's real config (the repository root) | The project's config or policies change |

The fixture is test data, not a set of agents for this project: it pins runtime behaviour so
runtime tests keep passing however the project's own agents, skills and policies evolve.
Never point runtime tests at the project's agents, and never add project agents to the fixture.

`test_project_guarantees.py` pins this project's safety guarantees (secrets unreadable,
shell denied, private networks blocked, approvals on external writes). If you change a policy
on purpose, update that test in the same change.

When you remove a tool family from the project (say `tools/database/`), delete its tests in
`tests/tools/` too.
