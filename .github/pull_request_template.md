<!-- Thanks for contributing to Ava. Keep this short — the checklist matters more
     than the prose. -->

## What this changes

<!-- One or two sentences. If it fixes an issue, "Fixes #123". -->

## Why

<!-- What was wrong, or what this enables. If the reasoning is non-obvious,
     prefer putting it in a code comment where the next reader will find it. -->

## How it was verified

<!-- What you actually ran, and on what. "pytest tests/ green" plus anything
     hardware-specific you could or could not check. Say what you could NOT
     verify — a stated gap is far more useful than a silent one. -->

---

- [ ] `python -m pytest tests/ -q` passes
- [ ] `ruff check .` passes
- [ ] SPA touched? `cd frontend && npm run lint && npm test && npm run build`,
      and the rebuilt `dist/` is committed (built on the Node in
      `frontend/.nvmrc`)
- [ ] SPA touched? `bash qa/run.sh --e2e` passes (a skip counts as a failure in CI)
- [ ] New route? `tests/_route_table.json` regenerated in this commit
- [ ] New optional capability? Registered in `ava_bridge/features.py` and gated
      with `features.preflight(...)`
- [ ] Commits are signed off (`git commit -s`) — see [`DCO`](../DCO)
- [ ] Nothing personal or machine-specific added: no home paths, hostnames,
      private project names, or prose asserting your own machine rather than the
      hardware class — including inside images (`tests/test_no_owner_identity.py`
      scans text only; media has to be checked by eye)
