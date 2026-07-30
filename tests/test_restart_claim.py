"""Ava must not say it restarted the bridge unless it did.

`systemctl --user restart ava-bridge.service` was fired-and-forgotten, and **that
unit ships nowhere** — `git ls-files` has no `.service` file, so it exists only on
the maintainer's own box. Popen with `start_new_session` and no captured output
makes the failure invisible, while the Hub appended " · restarting bridge" to the
message whenever a restart was *wanted*.

So every forker who approved a `.py` code change was told:

    Applied 1 file(s) · commit abc1234 · restarting bridge

and nothing restarted. The change sat on disk, unloaded, until they happened to
restart Ava themselves — which is the worst shape for this particular claim,
because the next thing they do is wonder why their edit had no effect.

There is no single correct restart on every install, which is why guessing was
wrong: under Docker the restart policy owns the process, under `ava up` in a
terminal there is no manager at all, and only a systemd install has a unit.
"""
from __future__ import annotations

from unittest import mock

import pytest

from ava_bridge import code_agent


def _plan(*, systemctl: str | None, active: str = "active", container: bool = False):
    with mock.patch("shutil.which", return_value=systemctl), \
         mock.patch("ava_bridge.auth.in_container", return_value=container), \
         mock.patch("subprocess.run",
                    return_value=type("P", (), {"stdout": active})()):
        return code_agent.restart_plan()


def test_an_active_unit_is_the_only_thing_that_earns_a_restart_claim() -> None:
    method, detail = _plan(systemctl="/bin/systemctl", active="active")
    assert method == "systemd" and detail.endswith(".service")


def test_a_unit_that_exists_but_is_INACTIVE_does_not_count() -> None:
    """`is-active` returning anything else means something else is running Ava."""
    assert _plan(systemctl="/bin/systemctl", active="inactive")[0] == "manual"
    assert _plan(systemctl="/bin/systemctl", active="failed")[0] == "manual"


def test_no_systemctl_in_a_container_says_so_and_names_the_command() -> None:
    method, detail = _plan(systemctl=None, container=True)
    assert method == "container"
    assert "docker compose restart" in detail, (
        "the container branch does not name the command that actually works, so "
        "the owner is told a restart is needed with no way to do it")


def test_no_mechanism_at_all_admits_it() -> None:
    method, detail = _plan(systemctl=None, container=False)
    assert method == "manual"
    assert "nothing here can restart" in detail
    assert "AVA_BRIDGE_UNIT" in detail, (
        "the manual branch does not mention the override, so a forker whose unit "
        "has another name has no way to be detected")


def test_restart_bridge_never_reports_ok_when_it_did_not_act() -> None:
    with mock.patch("shutil.which", return_value=None), \
         mock.patch("ava_bridge.auth.in_container", return_value=False):
        r = code_agent.restart_bridge()
    assert r["attempted"] is False and r["ok"] is False, r
    assert r["detail"], "a refusal with no reason is indistinguishable from a bug"


def test_the_user_message_reports_what_HAPPENED() -> None:
    """The string that carried the false claim."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "ava_bridge"
           / "learning_api.py").read_text(encoding="utf-8")

    # Structure, not string-absence: the ok-branch must be gated on the RESULT and
    # a not-ok branch must exist. A text scan trips on the fallback string inside
    # the message itself, which is the same mistake as grepping source for a token
    # the file also discusses.
    assert '_rr.get("ok")' in src, (
        "the restart message is not gated on restart_result['ok'], so it describes "
        "an intention rather than an outcome. On every install with no active "
        "systemd unit — i.e. every fork, since no .service file ships — no restart "
        "happens and the old code still claimed one.")
    assert "RESTART NEEDED" in src, (
        "there is no branch telling the owner a restart did NOT happen.")
    ok_at = src.index('_rr.get("ok")')
    needed_at = src.index("RESTART NEEDED")
    assert ok_at < needed_at, (
        "the RESTART NEEDED branch is not the else of the ok check, so both could "
        "fire or neither")


def test_no_caller_fires_systemctl_blind() -> None:
    """Both call sites did this independently; one implementation now."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for rel in ("ava_bridge/code_agent.py", "ava_bridge/coder.py"):
        src = (root / rel).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            flat = " ".join(str(getattr(a, "value", "")) for a in ast.walk(node)
                            if isinstance(a, ast.Constant))
            if "systemctl" in flat and "restart" in flat and "is-active" not in flat:
                fn = getattr(node.func, "attr", getattr(node.func, "id", ""))
                assert fn != "Popen" or rel.endswith("code_agent.py"), (
                    f"{rel} spawns a blind systemctl restart. Call "
                    "code_agent.restart_bridge(), which checks the unit is active "
                    "and reports what it did.")


@pytest.mark.parametrize("unit_env", ["ava.service", "my-ava-bridge.service"])
def test_the_unit_name_is_overridable(monkeypatch, unit_env) -> None:
    """A forker's unit will not be called ava-bridge.service."""
    monkeypatch.setenv("AVA_BRIDGE_UNIT", unit_env)
    with mock.patch("shutil.which", return_value="/bin/systemctl"), \
         mock.patch("subprocess.run",
                    return_value=type("P", (), {"stdout": "active"})()):
        assert code_agent.restart_plan() == ("systemd", unit_env)
