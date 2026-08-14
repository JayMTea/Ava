"""The closed vocabularies on a hardware model row, each in tracked files.

TWO vocabularies now, on the same rails: `state` (is this model live?) and
`relation` (whose is it?). Both are machine tokens the frontend turns into the
words the owner reads, and both fail SILENTLY when they drift — an unknown
`state` renders as "Not observable", and an unknown `relation` groups the row
under "Other programs holding memory", which for a third-party process is right
by luck and for Ava's own brain is a lie.

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
RELATION_TS = "frontend/src/components/hwModels.ts"
MODELS_PY = "ava_bridge/models.py"

_TRACKED = (HARDWARE, TYPES_TS, COPY_TS, RELATION_TS, MODELS_PY)


def _sources() -> dict[str, str]:
    have = set(tracked())  # also skips cleanly outside a git checkout
    missing = [p for p in _TRACKED if p not in have]
    assert not missing, (
        f"{missing} are not tracked — if a model-row vocabulary moved, point "
        "this guard at its new home rather than deleting it")
    return {p: (ROOT / p).read_text(encoding="utf-8") for p in _TRACKED}


def _declared(src: str, name: str = "_STATES") -> set[str]:
    """The `<name> = (...)` tuple in hardware.py."""
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == name for t in node.targets)):
            return {e.value for e in node.value.elts if isinstance(e.value, str)}
    raise AssertionError(f"no `{name} = (...)` literal in {HARDWARE}")


def _returned(src: str, fn_name: str) -> set[str]:
    """Every string literal a function can RETURN."""
    out: set[str] = set()
    for fn in ast.walk(ast.parse(src)):
        if isinstance(fn, ast.FunctionDef) and fn.name == fn_name:
            for node in ast.walk(fn):
                if (isinstance(node, ast.Return)
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    out.add(node.value.value)
    assert out, f"no string returns found in `{fn_name}` — did it move or change shape?"
    return {s for s in out if s}


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


def _union(src: str, name: str = "ModelState") -> set[str]:
    """The named union's members in types.ts."""
    m = re.search(rf"export type {name}\s*=(.*?);", src, re.S)
    assert m, f"no `export type {name}` in {TYPES_TS}"
    return set(re.findall(r"'([a-z_]+)'", m.group(1)))


def _copy_keys(src: str, const: str = "MODEL_STATE", where: str = COPY_TS) -> set[str]:
    """The named record's keys in a copy table."""
    m = re.search(rf"export const {const}[^{{]*\{{(.*?)\n\}};", src, re.S)
    assert m, f"no `export const {const}` in {where}"
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
        "label/hint the owner actually reads.")


def test_every_emitted_relation_is_in_the_declared_vocabulary() -> None:
    """Same trap as `_STATES`: without this, `_RELATIONS` is a comment.

    A new `return "sandbox"` in `_relation` would never touch the tuple, and
    `relationOf()` downgrades what it does not recognise to `foreign` — so the
    row would quietly be filed under "other software on this machine".
    """
    src = _sources()[HARDWARE]
    declared, emitted = _declared(src, "_RELATIONS"), _returned(src, "_relation")
    assert emitted <= declared, (
        f"{HARDWARE} emits relation(s) {sorted(emitted - declared)} that are not "
        f"in `_RELATIONS`. Add them there, to the `ModelRelation` union in "
        f"{TYPES_TS}, and to `MODEL_RELATION` in {RELATION_TS} with the heading "
        "the owner should read.")


def test_declared_relations_match_both_frontend_copies() -> None:
    src = _sources()
    declared = _declared(src[HARDWARE], "_RELATIONS")
    union = _union(src[TYPES_TS], "ModelRelation")
    keys = _copy_keys(src[RELATION_TS], "MODEL_RELATION", RELATION_TS)
    assert declared == union, (
        f"model-relation drift: `_RELATIONS` in {HARDWARE} is {sorted(declared)} "
        f"but the `ModelRelation` union in {TYPES_TS} is {sorted(union)}.")
    assert declared == keys, (
        f"model-relation drift: `_RELATIONS` in {HARDWARE} is {sorted(declared)} "
        f"but `MODEL_RELATION` in {RELATION_TS} covers {sorted(keys)}. Every "
        "relation needs the section heading the owner actually reads.")


def test_declared_truths_match_the_frontend_union() -> None:
    """The THIRD vocabulary on a model row: does config agree with reality?

    Same rails, same failure mode. `drift` is a machine token the frontend turns
    into the sentence the owner reads, and an unrecognised one renders as nothing
    at all — which is precisely the outcome the fact exists to prevent.

    This one earned its guard the hard way. `TRUTHS` gained `mismatched` on
    2026-08-13 after a model swap left the agent sandbox onboarded with the old
    id: nothing failed, because the router rewrites the model on the way through,
    but the panel named a model that was not answering. A verdict the TypeScript
    side does not know about is a verdict nobody is ever told.
    """
    src = _sources()
    declared = _declared(src[MODELS_PY], "TRUTHS")
    union = _union(src[TYPES_TS], "BrainTruth")
    assert declared == union, (
        f"brain-truth drift: `TRUTHS` in {MODELS_PY} is {sorted(declared)} but "
        f"the `BrainTruth` union in {TYPES_TS} is {sorted(union)}. Add the "
        "verdict to BOTH, and give it a line in `driftLine()` "
        f"({RELATION_TS}) — a verdict with no copy is one the owner never sees.")


def test_every_returned_truth_is_declared() -> None:
    """The non-vacuity half: `TRUTHS` must not be a comment that cannot fail.

    `serving_truth` returns its verdict as a literal in a dict, so a new branch
    can invent a token without touching `TRUTHS` and every test stays green.
    """
    src = _sources()[MODELS_PY]
    declared = _declared(src, "TRUTHS")
    emitted = set(re.findall(r'"verdict":\s*"([a-z_]+)"', src))
    extra = emitted - declared
    assert not extra, (
        f"`serving_truth` returns {sorted(extra)}, which is not in `TRUTHS`. "
        "A verdict outside the declared set reaches the frontend as an unknown "
        "token and renders as silence.")
    assert emitted, "found no verdict literals — this guard has stopped matching"
