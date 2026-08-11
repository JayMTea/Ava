"""The policy enumerator must see `generated/`, and be the only thing that globs.

Three callers globbed for policy files independently and disagreed. The
disagreement was not cosmetic:

  * `policy_mgmt.list_policies()` globbed `agent/policies/*.yaml`, so the four
    connector egress policies under `generated/` were absent from the only policy
    list the agent can see.
  * `ava_security_check.check_policy_wildcards()` globbed the same single level,
    so **an over-broad rule in a generated policy was never checked** — and those
    are the files written by code from connector manifests rather than reviewed by
    hand. That was a live hole, not an inventory gap:
    `test_a_wildcard_in_a_generated_policy_is_caught` plants one and it failed
    before this module existed.
  * `arch.actual_policies()` was the only one that walked both levels *and* knew
    that a generated policy is named by its `preset.name`, not its filename stem.

The last point is the subtle one. `generated/persona.yaml` declares
`preset.name: ava-persona`. Key anything on the stem and it refers to a policy the
sandbox does not have under that name.
"""
import subprocess
import sys

import pytest
import yaml

from gitfiles import ROOT, require_git, tracked

from ava_bridge import policy_inventory


# Modules allowed to glob for policy files. Anything else must go through the
# inventory, which is the mechanism that keeps the three views in agreement.
_GLOB_ALLOWED = {
    "ava_bridge/policy_inventory.py",   # the enumerator itself

    # --- the PRODUCER side, which is keyed on the connector id, not the set ---- #
    # These two write and drift-check `generated/<connector-id>.yaml`. They must
    # use the filename stem, because the stem IS the connector id — that is the
    # join key back to connectors/<id>/connector.yaml. The inventory reports
    # `preset.name` (`ava-persona`), which is deliberately NOT that key, so
    # routing the generator through it would break the lockstep check.
    #
    # The distinction to hold onto: reading "which policies exist" goes through
    # the inventory; writing or verifying ONE policy for a known connector does
    # not. A new module that enumerates in order to answer a question about the
    # policy set belongs in the inventory, not here.
    "ava_bridge/hub/connectors.py",     # renders + writes one policy per manifest
    "ava_cli.py",                       # `ava verify` lockstep drift check; writer
    # The path RESOLVER, one layer below the enumerator: settings owns where the
    # generated tree lives (under AVA_HOME, not the code root) and
    # policy_inventory now reads GENERATED_DIR from it. That is the opposite of
    # the failure this guard exists for — it is what makes the enumerator and
    # every writer agree on one directory instead of each joining their own.
    "ava_bridge/settings.py",
}

# Text that indicates a module is walking the policy tree on its own.
_GLOB_MARKERS = (
    "policies\").glob",
    "policies').glob",
    "POLICIES_DIR.glob",
    "POLICY_DIR.glob",
    "policies\", \"generated\"",
    "policies', 'generated'",
)


def _require_generated() -> list:
    """Generated policies are DERIVED artifacts, not source.

    `agent/policies/generated/` is gitignored (.gitignore:79) and nothing under
    it is tracked, so a fresh clone — and every CI job, which runs
    `pytest tests/` with no generation step — has an empty tree here. Asserting
    presence would make these three tests fail for everyone but the owner, which
    is worse than not running them: a suite that is red on checkout trains people
    to ignore it.

    The guards that do not depend on a deployed instance still run everywhere:
    the static glob scan and all three wildcard checks (which build their own
    tmp_path fixtures).
    """
    gen = [p for p in policy_inventory.inventory() if p.source == "generated"]
    if not gen:
        pytest.skip(
            "no generated policies on this box — agent/policies/generated/ is "
            "gitignored and derived. Populate it with `ava connector policies` "
            "to exercise this guard locally.")
    return gen


def test_the_generated_policies_are_in_the_inventory() -> None:
    gen = _require_generated()
    assert gen, (
        "no policies with source='generated' — the enumerator stopped descending "
        "into agent/policies/generated/, which is the exact bug this module "
        "exists to prevent.")


def test_a_generated_policy_is_named_by_its_preset_not_its_filename() -> None:
    """The detail that used to live in one comment in arch.py."""
    renamed = [p for p in _require_generated() if p.name != p.file_stem]
    assert renamed, (
        "every generated policy's preset.name equals its filename stem, so this "
        "test can no longer detect a reader that keys on the stem. If the "
        "generator changed to make them match, say so here; do not delete the "
        "distinction, because arch.py's drift check depends on the preset name.")
    for p in renamed:
        loaded = yaml.safe_load(open(p.path, encoding="utf-8"))
        assert p.name == loaded["preset"]["name"], (
            f"{p.rel}: inventory reports name={p.name!r} but preset.name is "
            f"{loaded['preset']['name']!r}")


def test_the_three_callers_agree_with_the_inventory() -> None:
    """The whole point: one enumerator, three consumers, no independent globs."""
    sys.path.insert(0, str(ROOT / "agent" / "docs"))
    import arch   # after the sys.path insert above

    from ava_bridge import policy_mgmt

    inv = policy_inventory.names()
    assert arch.actual_policies() == inv, (
        "arch.actual_policies() disagrees with the inventory, so the manifest "
        "drift check runs over a different policy set than the security check.")
    listed = {p["name"] for p in policy_mgmt.list_policies()["policies"]}
    assert listed == inv, (
        "policy_mgmt.list_policies() disagrees with the inventory. The agent's "
        f"own view of its egress policies is missing {sorted(inv - listed)} and "
        f"inventing {sorted(listed - inv)}.")


def test_only_the_inventory_walks_the_policy_tree() -> None:
    """A static scan — the mechanism, not the current result.

    Three independent globs is how they drifted in the first place. A fourth added
    next year would be caught here rather than by someone noticing that a policy
    is missing from one of three lists.
    """
    require_git()
    offenders = []
    for rel in tracked("*.py"):
        if rel in _GLOB_ALLOWED or rel.startswith(("tests/", "qa/")):
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(m in text for m in _GLOB_MARKERS):
            offenders.append(rel)
    assert not offenders, (
        f"these modules walk the policy tree themselves: {offenders}. Call "
        "ava_bridge.policy_inventory.inventory() (or .names()) instead — three "
        "independent globs is how generated/ became invisible to the security "
        "check. If a module genuinely must glob, add it to _GLOB_ALLOWED in this "
        "file with the reason.")


def test_a_wildcard_in_a_generated_policy_is_caught(tmp_path, monkeypatch) -> None:
    """The live security hole, reproduced.

    Before the rewire, `check_policy_wildcards` globbed one level, so this rule in
    a generated policy produced no error at all. The plant goes into a temp tree
    rather than the real `agent/policies/generated/`, because a test that edits a
    tracked policy file leaves the repo dirty if it fails midway.
    """
    import ava_security_check as sc

    gen = tmp_path / "agent" / "policies" / "generated"
    gen.mkdir(parents=True)
    (gen / "leaky.yaml").write_text(
        "preset:\n  name: ava-leaky\n"
        "network_policies:\n  ava-leaky:\n    name: ava-leaky\n"
        "    endpoints:\n    - host: host.openshell.internal\n      port: 8096\n"
        "      rules:\n      - allow: { method: GET, path: \"/internal/**\" }\n",
        encoding="utf-8")
    monkeypatch.setattr(policy_inventory, "ROOT", str(tmp_path))
    monkeypatch.setattr(policy_inventory, "POLICY_DIR",
                        str(tmp_path / "agent" / "policies"))
    monkeypatch.setattr(policy_inventory, "GENERATED_DIR", str(gen))
    monkeypatch.setattr(policy_inventory, "OVERLAY_DIR", str(tmp_path / "nope"))

    errors: list[str] = []
    sc.check_policy_wildcards(errors)
    assert any("generated" in e and "/internal/**" in e for e in errors), (
        "a whole-surface wildcard in a GENERATED policy produced no error: "
        f"{errors}. This is the hole the rewire closed — generated/ is written by "
        "code from connector manifests, so it is the least-reviewed directory and "
        "was the unchecked one.")


def test_a_wildcard_on_a_THIRD_PARTY_host_is_not_an_error(tmp_path, monkeypatch) -> None:
    """The other half, or the check just fails on `ava-weather` forever.

    `path: "/**"`, GET-only, on `api.open-meteo.com` is least privilege: you
    cannot enumerate someone else's API, and the host plus method IS the boundary.
    The substring scan this replaced flagged it, which is why the security check
    was failing on a clean tree — and a check that always fails is one nobody runs.
    """
    import ava_security_check as sc

    pol = tmp_path / "agent" / "policies"
    pol.mkdir(parents=True)
    (pol / "ext.yaml").write_text(
        "preset:\n  name: ava-ext\n"
        "network_policies:\n  ava-ext:\n    name: ava-ext\n"
        "    endpoints:\n    - host: api.open-meteo.com\n      port: 443\n"
        "      rules:\n      - allow: { method: GET, path: \"/**\" }\n",
        encoding="utf-8")
    monkeypatch.setattr(policy_inventory, "ROOT", str(tmp_path))
    monkeypatch.setattr(policy_inventory, "POLICY_DIR", str(pol))
    monkeypatch.setattr(policy_inventory, "GENERATED_DIR", str(pol / "generated"))
    monkeypatch.setattr(policy_inventory, "OVERLAY_DIR", str(tmp_path / "nope"))

    errors: list[str] = []
    sc.check_policy_wildcards(errors)
    assert not errors, (
        f"a GET-only wildcard on a named third-party host was flagged: {errors}. "
        "Host and method are the boundary there; flagging it is what made the "
        "security check fail on a clean checkout.")


def test_an_unparsable_policy_is_reported_not_skipped(tmp_path, monkeypatch) -> None:
    """A YAML error must not become a way to hide a rule from the checker."""
    import ava_security_check as sc

    pol = tmp_path / "agent" / "policies"
    pol.mkdir(parents=True)
    (pol / "broken.yaml").write_text(
        "preset:\n  name: x\n  bad: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr(policy_inventory, "ROOT", str(tmp_path))
    monkeypatch.setattr(policy_inventory, "POLICY_DIR", str(pol))
    monkeypatch.setattr(policy_inventory, "GENERATED_DIR", str(pol / "generated"))
    monkeypatch.setattr(policy_inventory, "OVERLAY_DIR", str(tmp_path / "nope"))

    inv = policy_inventory.inventory()
    assert len(inv) == 1 and inv[0].parse_error, "the parse failure was swallowed"
    errors: list[str] = []
    sc.check_policy_wildcards(errors)
    assert any("does not parse" in e for e in errors), (
        f"an unparsable policy produced no error: {errors}. Silently skipping it "
        "means a broken file is a way to get a rule past the wildcard check.")


def test_the_security_check_passes_on_the_tracked_policies() -> None:
    """It exited 1 on a clean checkout, and nothing ran it, so nobody knew.

    Scoped to the policy check on purpose: `main()` also inspects live port
    bindings, which are a fact about the machine, so running the whole thing here
    would fail on any box whose services bind a tailnet address.
    """
    import ava_security_check as sc

    errors: list[str] = []
    sc.check_policy_wildcards(errors)
    assert not errors, (
        "the tracked egress policies fail their own security check:\n  "
        + "\n  ".join(errors)
        + "\n\nSECURITY.md tells owners to run ./ava_security_check.py; it must "
          "pass on a clean tree or the instruction trains people to ignore it.")


@pytest.mark.parametrize("digest_of", ["declared", "generated"])
def test_every_policy_carries_a_content_digest(digest_of) -> None:
    """`ava attest` embeds these; a name without a digest evidences nothing."""
    pols = [p for p in policy_inventory.inventory() if p.source == digest_of]
    if not pols:
        pytest.skip(f"no {digest_of} policies on this box")
    for p in pols:
        assert len(p.sha256) == 64, f"{p.rel} has no sha256"


def test_the_inventory_never_raises_on_a_missing_tree(tmp_path, monkeypatch) -> None:
    """A fork with no overlay, or a checkout mid-clone, must not crash a route."""
    monkeypatch.setattr(policy_inventory, "ROOT", str(tmp_path))
    monkeypatch.setattr(policy_inventory, "POLICY_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr(policy_inventory, "GENERATED_DIR", str(tmp_path / "nope2"))
    monkeypatch.setattr(policy_inventory, "OVERLAY_DIR", str(tmp_path / "nope3"))
    assert policy_inventory.inventory() == []
    snap = policy_inventory.snapshot()
    assert snap["count"] == 0 and snap["wildcards"] == 0


def test_the_route_is_reachable_and_returns_facts() -> None:
    _require_generated()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ava_bridge.hub import governance

    app = FastAPI()
    app.include_router(governance.router)
    body = TestClient(app, base_url="http://localhost").get("/policies/inventory").json()
    assert body["count"] >= 1
    assert body["by_source"]["generated"] >= 1, (
        "the route does not report the generated policies, which are the ones "
        "nothing else showed")
    assert "policies" in body and body["policies"][0]["sha256"]


def test_the_checker_still_runs_as_a_script() -> None:
    """It is documented in SECURITY.md as `./ava_security_check.py`."""
    r = subprocess.run([sys.executable, str(ROOT / "ava_security_check.py")],
                       capture_output=True, text=True, timeout=120)
    assert "Traceback" not in r.stderr, (
        f"ava_security_check.py crashed rather than reporting findings:\n{r.stderr}")
    assert "policy" not in r.stdout.lower() or "FAILED" not in r.stdout, (
        f"the script reports a policy failure:\n{r.stdout}")
