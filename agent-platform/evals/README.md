# evals/

Evals answer "is it good, and is it still safe?" Tests answer "does the code work?"

| Folder | Holds | Runs |
|---|---|---|
| `datasets/*.jsonl` | Quality cases: input + graders, plus a `mock_script` | Offline (harness check) and on real routes (quality) |
| `scenarios/*.yaml` | Behaviour and safety cases that script the model to attempt something that must be refused | Offline only, in CI |
| `graders/` | `status`, `contains`, `not_contains`, `regex`, `tool_called`, `tool_not_called`, `tool_error_contains`, `llm_judge` | — |
| `regression/baseline.json` | Per route: minimum pass rate and the cases that must keep passing | Compared on every run |
| `benchmarks/` | Cost / latency / quality comparisons between routes | By hand, before model changes |

```bash
python -m evals.run --route test                    # offline; part of scripts/check.py
python -m evals.run --route test --update-baseline  # accept current results
python -m evals.run --suite smoke --route balanced  # real models - costs money
```

A run fails when a case that passed in the baseline now fails, or the pass rate drops
below the baseline's minimum. Results are written to `state/cache/evals/`.

Writing good cases: use real tasks (scrubbed), cover each agent with at least one edge
case, prefer deterministic graders, and keep one scenario per safety boundary. Calibrate
`llm_judge` rubrics against ~20 hand-graded outputs before trusting them.
