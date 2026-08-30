"""Fixture-contract net: the demo/ Playwright harness verifies the SPA
against hand-written fixtures. Nothing stops those fixtures drifting from the
real API — until this test. For every fixture with a known endpoint, every key
the fixture promises must exist in the REAL response shape (recursively; list
elements compared against the first real element when one exists).
"""
import json
import os
import unittest

import pytest

from qa import helpers

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(_REPO, "demo", "fixtures")

# fixture file -> live endpoint. Only pairs whose real endpoint returns the
# same object are listed; demo-only fixtures (chat-detail, brand variants) are
# checked when confidently mappable.
PAIRS = {
    "brand.json": "/api/brand",
    "apps.json": "/api/apps",
    "hardware.json": "/api/hardware",
    "budget.json": "/api/hub/cost",
    "turns.json": "/api/turns",
    "chats.json": "/api/chats",
    "hub-system.json": "/api/hub/system",
    "hub-connectors.json": "/api/hub/connectors",
    "hub-audit.json": "/api/hub/audit",
    "hub-approvals.json": "/api/hub/approvals",
    "setup-connectors.json": "/api/setup/connectors",
    "hub-voice.json": "/api/hub/voice/status",
    "hub-agent-status.json": "/api/hub/agent/status",
    "hub-backends.json": "/api/setup/backends",  # mock.ts serves it there
    "hub-models.json": "/api/hub/models",
    # Added when the memory/skills panels landed; the net had not been
    # extended with them. Endpoints taken from mock.ts's own route table,
    # not guessed.
    "hub-memory.json": "/api/hub/memory",
    "hub-skills.json": "/api/hub/agent/skills",
    # Added when Setup → Agent split into sub-panels and Persona got its own
    # mocked route (demo/src/engine/mock.ts serves it at the same path).
    "hub-persona.json": "/api/hub/persona",
}

# Subtrees whose keys are DATA (app names, status values, category buckets),
# not schema — a fixture may demo keys a fresh instance doesn't have yet.
DYNAMIC_KEYS = {
    "ops-summary.json": ("$.turns.by_status",),
    "perf-cost.json": ("$.by",),
    "perf-summary.json": ("$.sources_present", "$.summary"),
    # audit rows are heterogeneous per `kind` (turn/approval/chat_delete...):
    # the fixture's first row and the live ledger's rarely share a shape.
    "hub-audit.json": ("$.events",),
}


def _missing_keys(fixture, real, path="$", dynamic=()):
    """Keys promised by the fixture but absent from the real response."""
    out = []
    if path in dynamic or real is None:
        return out  # data-keyed subtree / null on a fresh instance: unprovable
    if isinstance(fixture, dict):
        if not isinstance(real, dict):
            out.append(f"{path}: fixture dict vs real {type(real).__name__}")
            return out
        for k, v in fixture.items():
            if k not in real:
                out.append(f"{path}.{k}")
            else:
                out.extend(_missing_keys(v, real[k], f"{path}.{k}", dynamic))
    elif isinstance(fixture, list) and fixture:
        if not isinstance(real, list):
            out.append(f"{path}: fixture list vs real {type(real).__name__}")
        elif real:  # empty real list proves nothing either way — skip
            out.extend(_missing_keys(fixture[0], real[0], f"{path}[0]", dynamic))
    return out


@pytest.fixture(scope="module", autouse=True)
def _client(bridge):
    helpers.ensure_authed(bridge)
    globals()["CLIENT"] = bridge
    yield


@pytest.mark.skipif(not os.path.isdir(FIXTURES),
                    reason="demo/ harness not present on this machine")
class TestFixtureContract(unittest.TestCase):
    def test_every_mapped_fixture_matches_the_real_shape(self):
        failures = []
        for name, endpoint in sorted(PAIRS.items()):
            fpath = os.path.join(FIXTURES, name)
            if not os.path.isfile(fpath):
                continue
            with open(fpath, encoding="utf-8") as f:
                fixture = json.load(f)
            r = CLIENT.get(endpoint)
            if r.status_code != 200:
                failures.append(f"{name}: GET {endpoint} -> {r.status_code}")
                continue
            missing = _missing_keys(fixture, r.json(),
                                    dynamic=DYNAMIC_KEYS.get(name, ()))
            if missing:
                failures.append(f"{name} vs {endpoint}: " + ", ".join(missing[:8]))
        self.assertEqual(failures, [],
                         "fixtures promise fields the real API no longer has:\n"
                         + "\n".join(failures))

    def test_all_fixtures_accounted_for(self):
        """Every fixture is either mapped above or on the explicit demo-only
        list — so a NEW fixture can't silently dodge the contract net."""
        # Each entry needs a REASON — "demo only" with no argument is how a
        # fixture that could be checked stops being checked.
        demo_only = {
            "model.json", "chat-detail.json", "hub-hardware.json",
            "hub-models.json",
            # /api/hub/connectors/{cid}/grants — path-parameterised, so a bare
            # GET cannot reach it and there is no connector-independent shape.
            "hub-grants.json",
            # The mock reshapes this one: it holds a map keyed by log name and
            # serves a member of it at /api/data/logs/{name}/tail, so the file
            # and the response are deliberately different objects.
            "data-logs.json",
        }
        actual = {f for f in os.listdir(FIXTURES) if f.endswith(".json")}
        unaccounted = actual - set(PAIRS) - demo_only
        self.assertFalse(
            unaccounted,
            f"these fixtures map to nothing: {unaccounted}. A NEW one needs a "
            "PAIRS mapping or a demo_only entry. A fixture for a route that no "
            "longer exists should be DELETED — the Vitals, Operations and Data "
            "pages were removed along with /api/perf/*, /api/ops/* and "
            "/api/data/*, so perf-*, ops-*, services/schedule/tools/alerts/"
            "connectors and data-* fixtures are stale.")
