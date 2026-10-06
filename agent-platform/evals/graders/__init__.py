"""Graders: each takes a grader spec from a case and the run's outcome, returns (ok, detail).

Deterministic graders are cheap and exact - use them for everything they can express.
Reach for `llm_judge` only for qualities a rule cannot check (helpfulness, tone, accuracy
of a free-form summary), and keep its rubric specific.

Outcome shape: {"status", "output", "error", "tool_calls": [tool ids],
                "tool_results": [{"tool", "is_error", "content"}]}
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .llm_judge import llm_judge

Grader = Callable[..., tuple[bool, str]]


def _contains(spec: dict[str, Any], out: dict[str, Any], **_: Any) -> tuple[bool, str]:
    ok = spec["value"].lower() in (out["output"] or "").lower()
    return ok, f"output should contain {spec['value']!r}"


def _not_contains(spec: dict[str, Any], out: dict[str, Any], **_: Any) -> tuple[bool, str]:
    ok = spec["value"].lower() not in (out["output"] or "").lower()
    return ok, f"output must not contain {spec['value']!r}"


def _regex(spec: dict[str, Any], out: dict[str, Any], **_: Any) -> tuple[bool, str]:
    return bool(re.search(spec["value"], out["output"] or "", re.MULTILINE)), f"output should match /{spec['value']}/"


def _status(spec: dict[str, Any], out: dict[str, Any], **_: Any) -> tuple[bool, str]:
    return out["status"] == spec["value"], f"status should be {spec['value']!r}, was {out['status']!r} ({out.get('error')})"


def _tool_called(spec: dict[str, Any], out: dict[str, Any], **_: Any) -> tuple[bool, str]:
    return spec["value"] in out["tool_calls"], f"{spec['value']} should have been called (calls: {out['tool_calls']})"


def _tool_not_called(spec: dict[str, Any], out: dict[str, Any], **_: Any) -> tuple[bool, str]:
    ok = not any(r["tool"] == spec["value"] and not r["is_error"] for r in out["tool_results"])
    return ok, f"{spec['value']} must not have run successfully"


def _tool_error_contains(spec: dict[str, Any], out: dict[str, Any], **_: Any) -> tuple[bool, str]:
    ok = any(
        r["tool"] == spec["tool"] and r["is_error"] and spec["value"].lower() in r["content"].lower()
        for r in out["tool_results"]
    )
    return ok, f"{spec['tool']} should have failed with {spec['value']!r}"


GRADERS: dict[str, Grader] = {
    "contains": _contains,
    "not_contains": _not_contains,
    "regex": _regex,
    "status": _status,
    "tool_called": _tool_called,
    "tool_not_called": _tool_not_called,
    "tool_error_contains": _tool_error_contains,
    "llm_judge": llm_judge,
}


def grade(spec: dict[str, Any], outcome: dict[str, Any], **context: Any) -> tuple[bool, str]:
    grader = GRADERS.get(spec["type"])
    if grader is None:
        return False, f"unknown grader type {spec['type']!r}"
    return grader(spec, outcome, **context)
