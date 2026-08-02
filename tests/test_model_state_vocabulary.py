"""The model-liveness vocabulary is ONE closed set, in three tracked files.

`state` on a hardware row is a machine token the frontend turns into the single
sentence it shows the owner. The set of tokens lives in three places by
necessity — `ava_bridge/hardware.py` `_STATES` (the definition), the
`ModelState` union in `frontend/src/lib/types.ts`, and the `MODEL_STATE` copy
table in `frontend/src/lib/modelState.ts` — and only the two TypeScript ones
are kept honest by a compiler (`Record<ModelState, StateCopy>` is exhaustive).
The Python side had nothing: `stateOf()` deliberately downgrades a token it
does not recognise to `unknown`, so a backend that starts emitting a seventh
state renders a perfectly healthy model as "Not observable — Ava cannot see
inside this runtime to check", with every test, lint and typecheck green.

Two assertions, because the obvious one alone is vacuous:

  1. Every state token hardware.py actually EMITS is in `_STATES`. Without
     this, `_STATES` is a comment that cannot fail — a new `return "loading"`
     never touches it, so a guard that only compares the three lists agrees
     with itself while the UI has already gone wrong.
  2. `_STATES`, the `ModelState` union and the `MODEL_STATE` keys are the same
     set, so adding a token to the definition forces the copy that renders it.

Guard style: a static scan over `git ls-files` (see tests/gitfiles.py), so it
runs anywhere and needs no bridge, no browser and no node.
"""
import ast
import pathlib
import re

from gitfiles import tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]

HARDWARE = "ava_bridge/hardware.py"
TYPES_TS = "frontend/src/lib/types.ts"
COPY_TS = "frontend/src/lib/modelState.ts"


def _sources() -> dict[str, str]:
    have = set(tracked())  # also skips cleanly outside a git checkout
    missing = [p for p in (HARDWARE, TYPES_TS, COPY_TS) if p not in have]
    assert not missing, (
        f"{missing} are not tracked — if the model-state vocabulary moved, point "
        "this guard at its new home rather than deleting it")
    return {p: (ROOT / p).read_text(encoding="utf-8") for p in (HARDWARE, TYPES_TS, COPY_TS)}


def _declared(src: str) -> set[str]:
    """The `_STATES = (...)` tuple in hardware.py."""
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "_STATES" for t in node.targets)):
            return {e.value for e in node.value.elts if isinstance(e.value, str)}
    raise AssertionError(f"no `_STATES = (...)` literal in {HARDWARE}")


def _emitted(src: str) -> set[str]:
    """Every string literal hardware.py can put in a row's `state`.

    Two shapes, both of which the module uses: the first slot of what
    `_backend_state` returns, and anything assigned to a `state` key/subscript
    (`"state": state if … else "offline"`, `row["state"] = …`).
    """
    tree = ast.parse(src)
    out: set[str] = set()

    def _strings(node: ast.AST) -> set[str]:
        """String literals this expression can EVALUATE to.

        Value position only: `("resident" if src == "nvidia-smi" else state)`
        yields `resident`, not the `nvidia-smi` it compares against. A `state`
        that comes from a name or a call is pinned by `_backend_state` above.
        """
        if isinstance(node, ast.Constant):
            return {node.value} if isinstance(node.value, str) else set()
        if isinstance(node, ast.IfExp):
            return _strings(node.body) | _strings(node.orelse)
        if isinstance(node, ast.BoolOp):
            return set().union(*(_strings(v) for v in node.values))
        return set()

    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == "_backend_state":
            for node in ast.walk(fn):
                if (isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)
                        and node.value.elts):
                    first = node.value.elts[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        out.add(first.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "state":
                    out |= _strings(v)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "state"):
                    out |= _strings(node.value)
    return {s for s in out if s}


def _union(src: str) -> set[str]:
    """The `ModelState` union members in types.ts."""
    m = re.search(r"export type ModelState\s*=(.*?);", src, re.S)
    assert m, f"no `export type ModelState` in {TYPES_TS}"
    return set(re.findall(r"'([a-z_]+)'", m.group(1)))


def _copy_keys(src: str) -> set[str]:
    """The `MODEL_STATE` record keys in modelState.ts."""
    m = re.search(r"export const MODEL_STATE[^{]*\{(.*?)\n\};", src, re.S)
    assert m, f"no `export const MODEL_STATE` in {COPY_TS}"
    return set(re.findall(r"^  ([a-z_]+):\s*\{", m.group(1), re.M))


def test_every_emitted_state_is_in_the_declared_vocabulary() -> None:
    src = _sources()[HARDWARE]
    declared, emitted = _declared(src), _emitted(src)
    assert emitted <= declared, (
        f"{HARDWARE} emits state(s) {sorted(emitted - declared)} that are not in "
        f"`_STATES`. Add them to `_STATES`, to the `ModelState` union in "
        f"{TYPES_TS}, and to `MODEL_STATE` in {COPY_TS} with the sentence the "
        "owner should read — an unlisted token renders as \"Not observable\".")


def test_declared_vocabulary_matches_both_frontend_copies() -> None:
    src = _sources()
    declared = _declared(src[HARDWARE])
    union, keys = _union(src[TYPES_TS]), _copy_keys(src[COPY_TS])
    assert declared == union, (
        f"model-state drift: `_STATES` in {HARDWARE} is {sorted(declared)} but the "
        f"`ModelState` union in {TYPES_TS} is {sorted(union)}. `stateOf()` shows an "
        "unlisted token as \"Not observable\", so this drift is silent at runtime.")
    assert declared == keys, (
        f"model-state drift: `_STATES` in {HARDWARE} is {sorted(declared)} but "
        f"`MODEL_STATE` in {COPY_TS} covers {sorted(keys)}. Every state needs the "
        "label/hint/tone the owner actually reads.")
