"""Turning one response body into one observation — with its state and its
provenance kept apart.

THE TWO FIELDS, AND WHY THEY ARE TWO. ``state`` answers *did a number arrive*;
``provenance`` answers *how good is the number that did*. Collapsing them is the
bug this module exists to prevent: an app that is merely unreachable would
otherwise be recorded as though the owner had stopped measuring, and a
five-year series cannot tell those apart after the fact.

THE RULE THAT OUTRANKS THE VALUE. If a source publishes a flag saying a field is
not measured, that flag wins over whatever number sits in the field. Real case
from the estate this was built against: an earnings field reads ``0`` while the
same payload's ``measured`` flag reads ``False``, because the underlying feed has
no rows. Recording "0, measured" there is not a rounding error, it is a
fabricated observation — and it is indistinguishable from a genuine zero
forever afterwards.

DECLARES IS A CEILING. A metric may declare the best provenance it could ever
have. Observed provenance may match it or come in weaker; it may never be
promoted above it. A literal in a config file cannot make a number better than
its source.

THE POINTER GRAMMAR IS SMALL ON PURPOSE. Dotted descent, ``[i]`` for an index,
``[k=v]`` for one equality select. No wildcards, no arithmetic, no functions.
Anything that cannot be reached with those is a signal that the app should
expose the number properly, and it is recorded as ``no_source`` with the blocker
named rather than being extracted by a cleverer expression that nobody can audit.
"""
from __future__ import annotations

import json
import re
from typing import Any

_SEG = re.compile(r"([^.\[\]]+)|\[(\d+)\]|\[([^=\]]+)=([^\]]*)\]")

_MISS = object()


def resolve(body: Any, pointer: str) -> tuple[Any, bool]:
    """(value, found). `found` distinguishes a real null from a missed path —
    the difference between "the app says there is no reading" and "this pointer
    is wrong", which must never render the same way."""
    if not pointer:
        return None, False
    cur = body
    for m in _SEG.finditer(pointer):
        key, idx, k, v = m.groups()
        if key is not None:
            if not isinstance(cur, dict) or key not in cur:
                return None, False
            cur = cur[key]
        elif idx is not None:
            if not isinstance(cur, list) or int(idx) >= len(cur):
                return None, False
            cur = cur[int(idx)]
        else:
            if not isinstance(cur, list):
                return None, False
            hit = next((r for r in cur
                        if isinstance(r, dict) and str(r.get(k)) == v), None)
            if hit is None:
                return None, False
            cur = hit
    return cur, True


def decode(body: Any, how: str | None) -> Any:
    """Unwrap a transport envelope. Only one form is supported: a tool result
    that carries its JSON as text inside a content list."""
    if how != "mcp_text" or not isinstance(body, dict):
        return body
    parts = body.get("content")
    if not isinstance(parts, list):
        return body
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text":
            try:
                return json.loads(part.get("text") or "")
            except (TypeError, ValueError):
                continue
    return body


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _floor(a: str, b: str) -> str:
    """The weaker of two provenances. `assumed` < `derived` < `measured`."""
    order = {"assumed": 0, "derived": 1, "measured": 2}
    return a if order.get(a, 0) <= order.get(b, 0) else b


def observe(metric: dict, body: Any, *, note: str = "") -> dict:
    """One observation for one metric from one response body.

    Never raises: a collector that dies on a surprising payload stops collecting
    every other metric behind it.
    """
    mid = metric.get("metric_id") or metric.get("id")
    unit = metric.get("unit")
    declared = metric.get("declares") or "measured"
    out = {"metric": mid, "unit": unit, "value": None, "n": None,
           "lo": None, "hi": None, "state": "unavailable",
           "provenance": None, "why": note or ""}

    # A metric the catalogue already knows nothing measures.
    if metric.get("state") == "no_source" or metric.get("instrumentable") is False:
        out["state"] = "no_source"
        out["why"] = metric.get("blocked_by") or metric.get("reason") or ""
        return out

    read = metric.get("read") or {}
    body = decode(body, metric.get("decode"))

    val, found = resolve(body, read.get("value") or "")
    if not found:
        out["state"] = "unavailable"
        out["why"] = f"pointer did not resolve: {read.get('value')!r}"
        return out

    # --- provenance BEFORE the value is believed --------------------------- #
    prov = declared
    pptr = read.get("provenance")
    if pptr:
        flag, pfound = resolve(body, pptr)
        pmap = metric.get("provenance_map") or {}
        if pfound and pmap:
            mapped = pmap.get(flag, pmap.get(str(flag).lower(), _MISS))
            if mapped is _MISS:
                mapped = None if flag in (False, "false", None) else declared
            if mapped is None:
                # The source says this is not a measurement. The number in the
                # field is not evidence of anything.
                out["state"] = "no_source"
                out["why"] = "the source flags this value as not measured"
                return out
            prov = _floor(str(mapped), declared)
        elif pfound and isinstance(flag, bool):
            prov = declared if flag else "assumed"
    out["provenance"] = _floor(prov, declared)

    n_raw, _ = resolve(body, read.get("n") or "") if read.get("n") else (None, False)
    n = _num(n_raw)
    out["n"] = int(n) if n is not None else None
    for bound in ("lo", "hi"):
        if read.get(bound):
            bv, _ = resolve(body, read[bound])
            out[bound] = _num(bv)

    # min_n FIRST. A metric below its sample floor is `insufficient` whether or
    # not the source also returned a null: the method is sound, the evidence is
    # thin, and that is a different fact from "the app could not answer".
    min_n = metric.get("min_n")
    if min_n and (out["n"] is None or out["n"] < int(min_n)):
        out["state"] = "insufficient"
        out["why"] = f"n={out['n']} below min_n={min_n}"
        out["provenance"] = None
        return out

    num = _num(val)
    if num is None:
        # The app answered and said it has no reading. That is a correct answer,
        # and it must stay distinguishable from a zero for as long as the series
        # exists. An interval may still be present without a point estimate.
        out["state"] = "no_source" if out["n"] == 0 and not metric.get("min_n") else "unavailable"
        if out["lo"] is not None or out["hi"] is not None:
            out["state"] = "insufficient"
            out["why"] = "bounds only — no point estimate"
        elif out["state"] == "no_source":
            out["why"] = "the source reports no samples"
        else:
            out["why"] = out["why"] or "the source returned no value"
        # Provenance qualifies a reading. With no reading there is nothing for
        # it to qualify, and leaving "measured" on an absence is how a gap gets
        # counted as a good measurement in a coverage figure.
        out["provenance"] = None
        return out

    scale = metric.get("scale")
    if scale:
        num = num * float(scale)
    out["value"] = num
    out["state"] = "ok"
    return out
