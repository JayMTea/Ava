"""Model bench/compare — run the same prompt on each backend, measure it.

Answers "did the new model run better?": streams one completion per configured
inference backend and records time-to-first-token, tokens/sec, completion
tokens, and total wall time. Hits each backend's OpenAI-compatible endpoint
directly (not through the router's fit selection) so each model is measured in
isolation. Shared by `ava models bench` (CLI) and the Hub Models "Compare"
button.
"""
from __future__ import annotations

import json
import time

DEFAULT_PROMPT = ("In two sentences, explain what makes a good cup of coffee.")


def backends() -> list[dict]:
    """The configured inference backends (id/url/model/engine/api_key)."""
    from . import router_app
    return router_app.load_backends()


def bench_one(b: dict, prompt: str, max_tokens: int = 200) -> dict:
    """Stream one completion from backend `b`; return timing metrics or error.
    Never raises — a dead backend returns {ok: False, error: ...}."""
    import requests
    url = b["url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if b.get("api_key"):
        headers["Authorization"] = "Bearer " + b["api_key"]
    body = {
        "model": b.get("model") or "",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.time()
    ttft = None
    chars = 0
    completion_tokens = 0
    try:
        with requests.post(url, json=body, headers=headers, stream=True, timeout=180) as r:
            r.raise_for_status()
            for raw in r.iter_lines():
                if not raw:
                    continue
                line = raw.decode(errors="ignore")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                choices = obj.get("choices") or []
                if choices:
                    piece = (choices[0].get("delta") or {}).get("content")
                    if piece:
                        if ttft is None:
                            ttft = time.time() - t0
                        chars += len(piece)
                usage = obj.get("usage") or {}
                if usage.get("completion_tokens"):
                    completion_tokens = int(usage["completion_tokens"])
        total = time.time() - t0
        gen = max(total - (ttft or 0), 1e-6)
        reported = completion_tokens > 0        # did the endpoint report usage?
        if not reported:
            completion_tokens = max(1, chars // 4)  # ~4 chars/token estimate
        return {
            "ok": True,
            "id": b["id"], "model": b.get("model") or b["id"], "engine": b.get("engine"),
            "ttft_ms": round((ttft or 0) * 1000, 1),
            "tok_s": round(completion_tokens / gen, 1),
            "tokens": completion_tokens,
            "total_s": round(total, 2),
            "estimated_tokens": not reported,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "id": b["id"], "model": b.get("model") or b["id"],
                "error": str(e)[:120]}


def bench(prompt: str = DEFAULT_PROMPT, only: list[str] | None = None,
          max_tokens: int = 200, on_result=None) -> dict:
    """Bench every backend (or the subset whose id/model matches `only`).

    `on_result(results_so_far, total)` is called after each backend finishes, so
    a caller (the Hub compare panel) can stream partial results and show each row
    filling in as it lands instead of waiting for the whole run.
    """
    bs = backends()
    if only:
        want = [s.strip().lower() for s in only if s.strip()]
        bs = [b for b in bs
              if any(w in b["id"].lower() or w in (b.get("model") or "").lower()
                     for w in want)]
    total = len(bs)
    results: list[dict] = []
    for b in bs:
        results.append(bench_one(b, prompt, max_tokens))
        if on_result:
            try:
                on_result(list(results), total)
            except Exception:  # noqa: BLE001 — a progress hook must never fail the run
                pass
    ok = [r for r in results if r.get("ok")]
    winner = max(ok, key=lambda r: r["tok_s"])["id"] if ok else None
    return {"prompt": prompt, "max_tokens": max_tokens,
            "results": results, "winner": winner, "backend_count": total}
