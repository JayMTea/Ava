"""Model-graded check against a rubric. Runs on the `reasoning` route; skipped offline.

Spec: {"type": "llm_judge", "rubric": "The answer names X and cites a source for it."}
Calibrate before trusting it: hand-grade ~20 outputs and confirm the judge agrees.
"""

from __future__ import annotations

from typing import Any

from runtime.types import Message

JUDGE_SYSTEM = (
    "You grade an AI agent's answer against a rubric. Judge only what the rubric asks. "
    "Reply with PASS or FAIL on the first line, then one sentence explaining why."
)


def llm_judge(spec: dict[str, Any], out: dict[str, Any], *, platform: Any = None, offline: bool = True, **_: Any) -> tuple[bool, str]:
    if offline or platform is None:
        return True, "llm_judge skipped offline"
    prompt = f"<rubric>\n{spec['rubric']}\n</rubric>\n\n<answer>\n{out['output']}\n</answer>"
    response = platform.router.complete(
        route=spec.get("route", "reasoning"),
        system=JUDGE_SYSTEM,
        messages=[Message("user", prompt)],
        tools=[],
        effort="medium",
        max_output_tokens=1024,
        data_class="internal",
        agent="eval-judge",
    )
    verdict = response.text.strip().splitlines()[0].upper() if response.text.strip() else ""
    return verdict.startswith("PASS"), f"judge: {response.text.strip()[:300]}"
