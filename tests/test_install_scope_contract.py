"""`agent/install.sh` is the only path that writes into the NemoClaw sandbox, and
every bug it has had was silent.

Three of them are pinned here because each exits 0, prints a green log, and shows
its symptom hours later somewhere else:

  * **SPECS_JSON built inside the deploy loop.** §4/5 does delete-then-recreate on
    every ``/^ava-/`` key in ``d.mcp.servers``, driven by ``SPECS_JSON``. If that
    array is ever empty — because someone scope-gated the deploy loop, or re-nested
    the construction — the register call UNREGISTERS EVERY AVA MCP SERVER, the
    overlay's private ones included. On bash 4.4+/Linux ``${SPECS[*]}`` on an empty
    array yields ``[]`` silently; only bash 3.2/macOS errors under ``set -u``, so a
    macOS forker would never reproduce it.
  * **``| grep -vE "$NOISE" | tail -N`` with no failure handling.** ``grep -v``
    exits 1 when it filters everything, ``pipefail`` propagates it, and ``set -e``
    then aborts an otherwise-successful deploy.
  * **``|| true`` on the CLI calls.** The fix for the above, applied to
    ``policy-add`` and ``skill install``, swallowed genuine failures instead — which
    is how ``ava-email-read-only`` sat unapplied with nothing in Ava reporting it.

Same shape as tests/test_diagram_sync.py and tests/test_bridge_port_resolves.py: a
static scan that needs no bridge, no AVA_HOME, no sandbox and no network.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "agent" / "install.sh"


def _lines() -> list[str]:
    return INSTALL_SH.read_text(encoding="utf-8").splitlines()


def _lineno(pattern: str) -> int:
    """1-indexed line of the first match, or 0."""
    rx = re.compile(pattern)
    for i, line in enumerate(_lines(), 1):
        if rx.search(line):
            return i
    return 0


def test_specs_json_is_built_outside_every_loop_and_conditional() -> None:
    """The assignment must sit at column 0 — i.e. not nested in a `for`/`if`."""
    for i, line in enumerate(_lines(), 1):
        if re.match(r"^SPECS_JSON=", line):
            break
        if re.match(r"^\s+SPECS_JSON=", line):
            raise AssertionError(
                f"agent/install.sh:{i} assigns SPECS_JSON while indented, which means "
                "it is inside a loop or conditional. If that block is ever skipped, "
                "SPECS_JSON is empty and the register call in §4/5 unregisters every "
                "ava-* MCP server — silently, with a zero exit code. Build the specs "
                "unconditionally in §2c discovery; it is a pure pass that needs no "
                "sandbox.")
    else:
        raise AssertionError("agent/install.sh no longer assigns SPECS_JSON at all.")


def test_specs_are_computed_before_the_deploy_loop_consumes_them() -> None:
    specs = _lineno(r"^SPECS_JSON=")
    deploy = _lineno(r"^# --- 3\.")
    assert specs and deploy, "the §3 marker or the SPECS_JSON assignment moved."
    assert specs < deploy, (
        f"SPECS_JSON is assigned at line {specs}, after the §3 deploy section starts "
        f"at line {deploy}. Deploying bytes and computing the registration specs are "
        "two different jobs: the first talks to the sandbox and is skippable, the "
        "second is pure and must always run.")


def test_an_empty_server_set_is_refused_rather_than_registered() -> None:
    """Belt and braces: even if the ordering guard above is defeated."""
    sh = INSTALL_SH.read_text(encoding="utf-8")
    assert re.search(r'\[\s*"\$SPECS_JSON"\s*=\s*"\[\]"\s*\]', sh), (
        'agent/install.sh no longer refuses an empty SPECS_JSON. Writing `[]` into '
        "the register call deletes every ava-* server from openclaw.json and adds "
        "none back. That must be a loud abort, never a silent no-op.")


def test_every_nemoclaw_call_reports_its_own_exit_status() -> None:
    """No raw `| grep -vE "$NOISE"` pipelines — they report grep's status, not the
    CLI's, in both directions."""
    offenders: list[str] = []
    for i, line in enumerate(_lines(), 1):
        if '"$NEMOCLAW"' not in line:
            continue
        if "grep -vE" in line or "| tail -" in line:
            offenders.append(f"  agent/install.sh:{i}: {line.strip()}")
    assert not offenders, (
        "these nemoclaw invocations filter their output inline:\n"
        + "\n".join(offenders)
        + "\n\nThat reports grep's exit status, not the CLI's. `grep -v` exits 1 when "
        "it filters EVERYTHING, so under pipefail a clean deploy aborts; adding "
        "`|| true` fixes that by swallowing real failures instead. Route it through "
        "the _run_cli helper, which captures the status first and filters second.")


def test_the_run_cli_helper_captures_status_before_filtering() -> None:
    sh = INSTALL_SH.read_text(encoding="utf-8")
    assert "_run_cli()" in sh, "the _run_cli helper is gone; see the test above."
    body = sh[sh.index("_run_cli()"):]
    body = body[: body.index("\n}\n") + 3]
    assert "local out rc" in body, (
        "_run_cli must declare `local out rc` on its own line. `local out=$(...)` "
        "makes $? the status of `local`, not of the command — which silently defeats "
        "the entire point of the helper.")
    assert "return $rc" in body, "_run_cli no longer returns the CLI's exit status."


def test_a_rejected_policy_is_reported_rather_than_swallowed() -> None:
    sh = INSTALL_SH.read_text(encoding="utf-8")
    assert "POLICY_FAILED" in sh, (
        "agent/install.sh no longer counts failed policy applications. A rejected "
        "egress policy leaves the tools that depend on it blocked by deny-by-default, "
        "and it used to be invisible: `policy-add … || true` is exactly how "
        "ava-email-read-only stayed unapplied with nothing reporting it.")
    assert re.search(r"policy-add[\s\S]{0,400}?WARNING", sh), (
        "the policy-add failure path no longer prints a warning.")


def test_nullglob_is_set_before_the_first_glob_that_relies_on_it() -> None:
    nullglob = _lineno(r"^shopt -s nullglob")
    assert nullglob, "agent/install.sh no longer sets nullglob."
    first_glob = min(
        n for n in (
            _lineno(r'for d in "\$base"/mcp_server_\*/'),
            _lineno(r'for sk in "\$skroot"/\*/'),
            _lineno(r'for pol in "\$poldir"/\*\.yaml'),
        ) if n
    )
    assert nullglob < first_glob, (
        f"nullglob is set at line {nullglob} but the first glob that depends on it is "
        f"at line {first_glob}. It used to live inside §1, so skipping the policy "
        "section would turn it off for the server and skill discovery that follow, "
        "and an unmatched glob would be iterated as a literal path.")


def test_the_proxy_fallback_is_actually_reachable() -> None:
    """§2 discovers the sandbox's HTTPS_PROXY and falls back to the OpenShell
    default when there isn't one. That fallback was dead code for as long as it
    existed: `grep -oE` exits 1 on no-match, pipefail made that the command
    substitution's status, and `set -e` aborted the install one line before the
    fallback ran — with stderr redirected to /dev/null, so it died in silence.

    No-match is not the edge case, either. It is what a stopped sandbox does.
    """
    lines = _lines()
    # The code line, not the comment above it that explains all this.
    idx = next((i for i, ln in enumerate(lines)
                if "HTTPS_PROXY" in ln and not ln.lstrip().startswith("#")), None)
    assert idx is not None, "the §2 proxy discovery is gone."
    # The assignment spans a line continuation; take the statement.
    stmt = "\n".join(lines[idx:idx + 3])
    assert "|| true" in stmt, (
        "the proxy discovery no longer tolerates a no-match:\n"
        f"{stmt}\n\n"
        "Without `|| true`, an empty HTTPS_PROXY (i.e. a stopped sandbox) aborts "
        "the whole install here, silently, and the documented fallback on the next "
        "line never runs.")
    fallback = next((ln for ln in lines[idx:idx + 8] if "PROXY:-" in ln), "")
    assert fallback, "the proxy fallback default is gone."


def test_recover_keeps_the_redirects_that_stop_it_hanging() -> None:
    """Do not 'clean this up'. `recover` spawns a detached `ssh -f` that inherits
    our stdout; if that fd is a pipe, install.sh appears to hang forever."""
    line = next((ln for ln in _lines() if "recover" in ln and "timeout" in ln), "")
    assert line, "the §7 gateway recover call is gone."
    assert "</dev/null" in line and ">" in line, (
        f"the recover call lost its redirects: {line.strip()!r}. Both are load-bearing "
        "— see the comment above it.")
