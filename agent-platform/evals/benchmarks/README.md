# Benchmarks

Cost, latency and quality measurements against **real** model routes — the numbers you use
to choose models, effort levels and routes. Not run in CI (they cost money).

Suggested practice:

- Pick 20–50 representative tasks from production traffic (scrubbed) as a dataset in
  `evals/datasets/` with a `benchmark` suite tag.
- Run them per candidate route: `python -m evals.run --suite benchmark --route balanced`.
- Record per route: pass rate, median and p95 duration, input/output tokens per task, and
  cost per *completed* task (a cheaper call that needs retries is not cheaper).
- Commit the summary here as `YYYY-MM-DD-<route>.md` so model decisions have evidence.
- Change one variable at a time (model, effort, prompt) and re-run before deciding.
