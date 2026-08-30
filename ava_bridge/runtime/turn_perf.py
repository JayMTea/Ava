"""One honest `llm` perf record per agent turn — written the same way by every
runtime that serves one.

Agent turns bypass the inference router, which is the historical sole writer of
`llm` records. Without this, the throughput and routing figures stay empty
forever on a default install while the owner chats all day.

It lives here rather than on an adapter because there are now two runtimes that
serve turns (the CLI one and the gateway one) and a third that proxies to the
first. Two copies of "what an agent turn looks like in the perf log" would drift, and
the drift would be invisible: the panels would keep rendering, just with two
different ideas of what `source="agent"` means.

The discipline is unchanged and load-bearing: record only facts we HAVE —
duration, the model the sandbox is running, and token usage when the runtime
reports it. Nothing is inferred, and nothing raises into a turn.
"""
from __future__ import annotations

try:  # perf logging is optional; a missing module must not break a turn
    import perf_log
except Exception:  # noqa: BLE001
    perf_log = None  # type: ignore[assignment]


def log_turn(seconds: float, *, model: str | None,
             usage: dict | None = None) -> None:
    """Write the record. Never raises.

    `usage` is whatever the runtime reported, in whatever spelling it used —
    OpenAI's `prompt_tokens`/`completion_tokens` and the newer
    `input_tokens`/`output_tokens` both appear in the wild, so both are read and
    neither is required. A turn with no usage still gets a duration, which is
    the number the throughput panel actually needs.
    """
    if perf_log is None:
        return
    try:
        usage = usage if isinstance(usage, dict) else {}
        ct = usage.get("completion_tokens") or usage.get("output_tokens")
        pt = usage.get("prompt_tokens") or usage.get("input_tokens")
        perf_log.log_perf(
            "llm",
            served_model=model,
            served_label=(str(model).split("/")[-1] if model else "agent sandbox"),
            gen_seconds=round(seconds, 3),
            source="agent",
            prompt_tokens=pt if isinstance(pt, int) else None,
            completion_tokens=ct if isinstance(ct, int) else None,
            tokens_per_sec=(perf_log.tok_per_sec(ct, seconds)
                            if isinstance(ct, int) else None),
        )
    except Exception:  # noqa: BLE001 — telemetry must never break a turn
        pass
