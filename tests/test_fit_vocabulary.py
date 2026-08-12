"""The model-fit verdict is a closed vocabulary, in two tracked files.

`ava_bridge/model_fit.py` `FIT_VERDICTS` defines the tokens; the `FitVerdict`
union in `frontend/src/lib/modelFit.ts` mirrors them, and `fitLine()` turns each
into the one clause a model row says about memory.

This drifts SILENTLY, and worse than most. `fitLine()` returns null for any
token it does not recognise — which is correct (a UI must not invent copy for a
verdict it predates) and is also why a fifth verdict added on the Python side
would render as NOTHING AT ALL. The row would go quiet in exactly the case the
feature exists for: a model too big for the box, saying nothing about it.

Two assertions, because the obvious one alone is vacuous:

  1. Every verdict `assess()` can actually RETURN is in `FIT_VERDICTS`. Without
     this the tuple is a comment — a new `"verdict": "needs_offload"` never
     touches it, so a guard that only compares the two lists agrees with itself
     while the UI has already gone silent.
  2. `FIT_VERDICTS` and the `FitVerdict` union are the same set, so adding a
     token forces the copy that renders it.

Guard style: a static scan over `git ls-files` (see tests/gitfiles.py), so it
runs anywhere and needs no bridge, no browser and no node.
"""
import ast
import pathlib
import re

from gitfiles import tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]

FIT_PY = "ava_bridge/model_fit.py"
FIT_TS = "frontend/src/lib/modelFit.ts"


def _sources() -> dict[str, str] | None:
    have = set(tracked())  # also skips cleanly outside a git checkout
    if not have:
        return None
    missing = [p for p in (FIT_PY, FIT_TS) if p not in have]
    assert not missing, (
        f"{missing} are not tracked — if the fit vocabulary moved, point this "
        "guard at its new home rather than deleting it")
    return {p: (ROOT / p).read_text(encoding="utf-8") for p in (FIT_PY, FIT_TS)}


def _declared(src: str) -> set[str]:
    """The `FIT_VERDICTS = (...)` tuple."""
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "FIT_VERDICTS" for t in node.targets)):
            return {e.value for e in node.value.elts if isinstance(e.value, str)}
    raise AssertionError(f"no `FIT_VERDICTS = (...)` literal in {FIT_PY}")


def _emitted(src: str) -> set[str]:
    """Every string literal `assess()` can put in a row's `verdict`.

    Both shapes the function uses: a `"verdict": "…"` pair in a returned dict,
    and the `{**base, "verdict": "…"}` spread form.
    """
    out: set[str] = set()
    for fn in ast.walk(ast.parse(src)):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == "assess"):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "verdict"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    out.add(v.value)
                # `"verdict": "wont_fit" if unified else "will_spill"`
                if (isinstance(k, ast.Constant) and k.value == "verdict"
                        and isinstance(v, ast.IfExp)):
                    for branch in (v.body, v.orelse):
                        if isinstance(branch, ast.Constant) and isinstance(branch.value, str):
                            out.add(branch.value)
    assert out, "no `verdict` literals found in `assess` — did it move or change shape?"
    return out


def _union(src: str) -> set[str]:
    """The `FitVerdict` union in modelFit.ts."""
    m = re.search(r"export type FitVerdict\s*=\s*([^;]+);", src)
    assert m, f"no `export type FitVerdict = …` in {FIT_TS}"
    return set(re.findall(r"'([a-z_]+)'", m.group(1)))


def test_every_verdict_assess_returns_is_declared() -> None:
    src = _sources()
    if src is None:
        return  # not a git checkout
    emitted, declared = _emitted(src[FIT_PY]), _declared(src[FIT_PY])
    extra = sorted(emitted - declared)
    assert not extra, (
        f"`assess()` in {FIT_PY} returns {extra}, which `FIT_VERDICTS` does not "
        "declare. The frontend renders an unrecognised verdict as SILENCE, so "
        "this row would say nothing at all rather than say something wrong.")


def test_the_two_sides_declare_the_same_vocabulary() -> None:
    src = _sources()
    if src is None:
        return  # not a git checkout
    declared, union = _declared(src[FIT_PY]), _union(src[FIT_TS])
    assert declared == union, (
        f"fit-verdict drift: `FIT_VERDICTS` in {FIT_PY} is {sorted(declared)} but "
        f"the `FitVerdict` union in {FIT_TS} is {sorted(union)}. Every verdict "
        "needs the clause that renders it — `fitLine()` returns null for a token "
        "it does not know, which is silence, not an error.")
