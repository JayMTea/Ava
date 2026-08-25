"""Ava's run-event vocabulary, held to one shared contract.

The four kinds `iter_run` yields are spelled TWICE, in two languages, in files
that never import each other: the runtime adapters emit them, and
`frontend/src/lib/chatEvents.ts` folds them into the transcript. Python proved
the emit. Vitest proved the parse. Nothing proved they AGREE — so a fifth kind,
a renamed field, or a dropped one would pass both suites and break a live turn,
which is the one place nobody is looking.

`qa/fakes/run-events.json` is the contract. This file holds the Python side to
it; `chatEvents.contract.test.ts` folds every sample through the real reducer.

House style: stdlib unittest, static scan, no bridge, no network.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(ROOT, "qa", "fakes", "run-events.json")

with open(CONTRACT, encoding="utf-8") as f:
    DOC = json.load(f)

KINDS = set(DOC["kinds"])
# The kinds a `step` event carries INSIDE it (`{"kind": "step", "step": {"kind":
# <one of these>}}`). Read from the contract, not spelled here: this file used
# to hold its own copy ({"text", "tool", "reasoning", "thinking"}), and when the
# adapter grew a `tool_result` step — folded by the client all along — the copy
# reported it as an unknown RUN kind. Two copies of one vocabulary was the bug
# the contract file exists to prevent.
STEP_KINDS = set(DOC.get("step_kinds") or [])


def _emitters():
    """Files that yield RUN events: the runtime adapters and the turn path.

    Scoped deliberately. A whole-tree scan for `{"kind": ...}` also finds an
    unrelated `{"kind": "unknown"}` in the connectors hub — "kind" is an
    ordinary word, and a guard that reports it teaches people to widen the
    contract rather than narrow the scan. Found by `git ls-files` inside that
    scope, so a new adapter is covered the day it lands.
    """
    out = subprocess.run(["git", "ls-files", "ava_bridge/runtime",
                          "ava_bridge/turns.py"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    for rel in out.split():
        if rel.endswith(".py"):
            yield rel


def _emitted_kinds(body: str) -> set[str]:
    """Every literal `{"kind": "..."}` a module constructs, by PARSING.

    Literal-only and parsed, for the same reason the name-dispatch guard is:
    a docstring listing the vocabulary (base.py has one) is documentation, not
    an emission, and a regex cannot tell them apart.
    """
    found: set[str] = set()
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return found
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == "kind"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)):
                found.add(value.value)
    return found


class ContractTests(unittest.TestCase):

    def test_the_contract_is_substantial(self):
        """Anti-vacuous: an empty contract would make everything below pass."""
        self.assertGreaterEqual(len(KINDS), 4)
        self.assertGreaterEqual(len(DOC["samples"]), 4)

    def test_every_kind_has_at_least_one_sample(self):
        """A kind with no sample is a kind the TS side is never asked to fold."""
        sampled = {s["kind"] for s in DOC["samples"]}
        self.assertEqual(sampled, KINDS,
                         "every kind needs a sample, and every sample a kind")

    def test_every_sample_carries_its_required_fields(self):
        for sample in DOC["samples"]:
            for field in DOC["required_fields"][sample["kind"]]:
                self.assertIn(field, sample,
                              f"{sample['kind']} sample is missing {field}")

    def test_the_contract_lists_the_step_kinds(self):
        """Anti-vacuous for the emitter guard below: with no step kinds listed,
        `seen -= STEP_KINDS` subtracts nothing and every step kind the backend
        constructs would be reported as a run kind."""
        self.assertGreaterEqual(len(STEP_KINDS), 4)
        self.assertEqual(STEP_KINDS & KINDS, set(),
                         "a step kind cannot also be a run kind — the client "
                         "dispatches on the outer `kind` and would misroute it")

    def test_every_step_kind_has_a_step_sample(self):
        """A step kind with no sample is one the TS side never folds."""
        sampled = {s["step"]["kind"] for s in DOC["samples"]
                   if s["kind"] == "step"}
        self.assertEqual(sampled, STEP_KINDS,
                         "every step kind needs a `step` sample carrying it, "
                         "and every step sample a listed kind")

    def test_every_step_sample_carries_its_required_fields(self):
        for sample in DOC["samples"]:
            if sample["kind"] != "step":
                continue
            step = sample["step"]
            for field in DOC["step_required_fields"][step["kind"]]:
                self.assertIn(field, step,
                              f"step sample {step['kind']} is missing {field}")


class EmitterTests(unittest.TestCase):

    def test_the_backend_emits_exactly_the_contracted_kinds(self):
        """The failure this catches: a fifth kind added backend-side that the
        client silently drops on the floor, and a kind removed that the client
        still branches on."""
        seen: set[str] = set()
        where: dict[str, str] = {}
        for rel in _emitters():
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                for kind in _emitted_kinds(f.read()):
                    seen.add(kind)
                    where.setdefault(kind, rel)
        # Inside the run path, a literal "kind" IS a run kind — except the cot
        # STEP kinds, which ride INSIDE a step event rather than being events
        # themselves. The contract lists those too (`step_kinds`), so a step
        # kind the backend adds without telling the contract still fails here,
        # as an unknown run kind — which is the right alarm, because the client
        # will not fold it either.
        seen -= STEP_KINDS
        self.assertTrue(seen, "found no run-event emissions at all — the parse "
                              "has drifted and this guard is vacuous")
        extra = sorted(k for k in seen if k not in KINDS)
        self.assertEqual(
            extra, [],
            "the backend emits a run kind the contract does not list, so the "
            "client will drop it: " + ", ".join(f"{k} ({where[k]})"
                                                for k in extra))
        missing = sorted(KINDS - seen)
        self.assertEqual(
            missing, [],
            "the contract lists a kind nothing emits any more — remove it here "
            f"and from chatEvents.ts, or restore the emission: {missing}")


if __name__ == "__main__":
    unittest.main()
