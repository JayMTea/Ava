"""Eval runner. Gates releases: exits non-zero on any regression against the baseline.

    python -m evals.run                      # offline: mock model, every suite
    python -m evals.run --suite smoke        # one suite
    python -m evals.run --route reasoning    # real models (costs money; needs keys)
    python -m evals.run --update-baseline    # accept current results as the new baseline
    python -m evals.run --root DIR           # another platform root (e.g. examples/reference)

Two kinds of cases, same graders:
- datasets/*.jsonl   quality cases. Offline they replay `mock_script` to prove the harness;
                     on a real route the script is ignored and the model is graded.
- scenarios/*.yaml   behaviour and safety cases (policy denials, approvals, SSRF). They
                     script the model on purpose, so they only run offline (requires_mock).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from evals.graders import grade
from runtime import AllowListApprover, MockProvider, Platform, RunResult, load_platform

ROOT = Path(__file__).resolve().parent.parent


def load_cases(evals_dir: Path, suite: str | None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted((evals_dir / "datasets").glob("*.jsonl")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.strip():
                cases.append({**json.loads(line), "_source": f"{path.name}:{n}"})
    for path in sorted((evals_dir / "scenarios").glob("*.yaml")):
        cases.append({**yaml.safe_load(path.read_text(encoding="utf-8")), "_source": path.name, "requires_mock": True})
    ids = [c["id"] for c in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise SystemExit(f"duplicate eval ids: {sorted(duplicates)}")
    return [c for c in cases if suite is None or suite in c.get("suites", ["default"])]


def outcome_of(result: Any) -> dict[str, Any]:
    if isinstance(result, RunResult):
        calls = {c.id: c.name for m in result.messages if m.role == "assistant" for c in m.tool_calls}
        results = [
            {"tool": calls.get(m.tool_call_id, "?"), "is_error": m.is_error, "content": m.content}
            for m in result.messages
            if m.role == "tool"
        ]
        return {"status": result.status, "output": result.output, "error": result.error, "tool_calls": list(calls.values()), "tool_results": results}
    return {"status": result.status, "output": result.output, "error": result.failed_step, "tool_calls": [], "tool_results": []}


def run_case(case: dict[str, Any], config: Any, route: str, state_dir: Path) -> dict[str, Any]:
    offline = route == "test"
    if case.get("requires_mock") and not offline:
        return {"id": case["id"], "skipped": "scripted scenario; runs only with --route test"}
    providers = {"mock": MockProvider(script=case.get("mock_script", []))} if offline else None
    platform = Platform(
        config,
        state_dir=state_dir / case["id"],
        approver=AllowListApprover(set(case.get("approve", []))),
        providers=providers,
        route_override=route,
    )
    if "workflow" in case:
        result = platform.run_workflow(case["workflow"], case.get("inputs", {}))
    else:
        result = platform.run(case["input"], agent=case.get("agent"))
    outcome = outcome_of(result)
    checks = [grade(spec, outcome, platform=platform, offline=offline) for spec in case.get("graders", [])]
    return {
        "id": case["id"],
        "source": case["_source"],
        "passed": all(ok for ok, _ in checks),
        "checks": [{"ok": ok, "detail": detail} for ok, detail in checks],
        "status": outcome["status"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run")
    parser.add_argument("--suite")
    parser.add_argument("--route", default="test", help="model route; 'test' = offline mock")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--root", default=str(ROOT), help="platform root (directory with agent.yaml)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    evals_dir = root / "evals"
    config = load_platform(root)
    cases = load_cases(evals_dir, args.suite)
    scratch = Path(tempfile.mkdtemp(prefix="evals-"))
    try:
        results = [run_case(c, config, args.route, scratch) for c in cases]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    graded = [r for r in results if "skipped" not in r]
    passed = [r for r in graded if r["passed"]]
    for r in results:
        if "skipped" in r:
            print(f"SKIP  {r['id']}: {r['skipped']}")
            continue
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']}  [{r['status']}]")
        for check in r["checks"]:
            if not check["ok"]:
                print(f"        - {check['detail']}")
    rate = len(passed) / len(graded) if graded else 1.0
    print(f"\n{len(passed)}/{len(graded)} passed ({rate:.0%}) on route {args.route!r}")

    out_dir = root / "state" / "cache" / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    (out_dir / f"{stamp}-{args.route}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    baseline_path = evals_dir / "regression" / "baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else {}
    route_base = baseline.get("routes", {}).get(args.route, {})
    if args.update_baseline:
        baseline.setdefault("routes", {})[args.route] = {
            "min_pass_rate": round(rate, 3),
            "passing": sorted(r["id"] for r in passed),
        }
        baseline_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
        print(f"baseline updated for route {args.route!r}")
        return 0
    regressions = sorted(set(route_base.get("passing", [])) - {r["id"] for r in passed} & {r["id"] for r in graded})
    if regressions:
        print(f"REGRESSION: previously passing cases now fail: {regressions}")
    if rate < route_base.get("min_pass_rate", 0):
        print(f"REGRESSION: pass rate {rate:.0%} is below baseline {route_base['min_pass_rate']:.0%}")
    return 1 if regressions or rate < route_base.get("min_pass_rate", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
