"""The MCP capability groups are declared in three places; they must agree.

`agent/install.sh` discovers the groups by globbing `agent/mcp_server_*/` and
mints one HMAC token per discovered category. Two Python tables then have to
match that discovery, and both are hand-maintained:

  * `ava_bridge/internal.py` `_TOKEN_GROUPS`   — the tokens the bridge accepts
  * `ava_bridge/security.py` `INTERNAL_SCOPE_GROUPS` — what each group may reach

Both failure directions are silent and both present as a broken tool rather than
as an error:

  * a server whose group is missing from `_TOKEN_GROUPS` is handed no token;
  * a server whose group is missing from `INTERNAL_SCOPE_GROUPS` gets
    `frozenset()` from `internal.group_may`, so EVERY route is refused.

`internal.py` says these "must stay in sync with the `groups` list in
agent/install.sh (both derive from the discovered mcp_server_<group>
categories)" — this guard is what makes that sentence true.

Style matches tests/test_diagram_sync.py: a static scan over `git ls-files`. The
Python tables are read with `ast` rather than imported, so this runs with no
fastapi, no AVA_HOME and no bridge.
"""
import ast

from gitfiles import ROOT, require_git, tracked

INTERNAL = "ava_bridge/internal.py"
SECURITY = "ava_bridge/security.py"

# Groups the tables declare that have no `agent/mcp_server_<group>/` directory.
#
# `wellness` is a reservation: it is scoped and tokenised, but ships no server,
# so install.sh never discovers it and never mints its token. Harmless — nothing
# can present a token that was never issued — but it is a claim the tree makes
# and does not keep, so it is written down here rather than left to be
# rediscovered. Remove the entry when `agent/mcp_server_wellness/` lands, or drop
# the group from both tables.
_DECLARED_WITHOUT_SERVER = {"wellness"}


def _read(rel: str) -> str:
    require_git()
    assert rel in tracked(rel), f"{rel} is not tracked — did it move?"
    return (ROOT / rel).read_text(encoding="utf-8")


def _discovered_groups() -> set[str]:
    """The groups agent/install.sh would find: one per mcp_server_*/_server.mjs."""
    found = {
        path.split("/")[1][len("mcp_server_"):]
        for path in tracked("agent/mcp_server_*/_server.mjs")
    }
    assert found, (
        "no agent/mcp_server_*/_server.mjs found. install.sh exits 1 in this "
        "situation rather than registering zero servers, so if this is genuinely "
        "true the build is already broken; more likely the layout moved and this "
        "guard needs updating."
    )
    return found


def _token_groups() -> set[str]:
    """The `_TOKEN_GROUPS` list literal, before the optional overlay extends it."""
    for node in ast.walk(ast.parse(_read(INTERNAL))):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_TOKEN_GROUPS" for t in node.targets)
                and isinstance(node.value, ast.List)):
            found = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
            assert found, f"_TOKEN_GROUPS in {INTERNAL} parsed to zero entries"
            return found
    raise AssertionError(
        f"no `_TOKEN_GROUPS = [...]` list literal in {INTERNAL}. This guard reads "
        "the core list before the overlay's extend_token_groups() call, so a "
        "change in that shape disables the check — hence a failure, not an empty "
        "set. Update tests/test_internal_group_ssot.py alongside the change."
    )


def _scope_groups() -> set[str]:
    for node in ast.walk(ast.parse(_read(SECURITY))):
        if (isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "INTERNAL_SCOPE_GROUPS"
                and isinstance(node.value, ast.Dict)):
            found = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
            assert found, f"INTERNAL_SCOPE_GROUPS in {SECURITY} parsed to zero keys"
            return found
    raise AssertionError(f"no `INTERNAL_SCOPE_GROUPS: ... = {{...}}` in {SECURITY}")


# ── the invariants ──────────────────────────────────────────────────────────

def test_every_shipped_server_has_a_token_group() -> None:
    missing = sorted(_discovered_groups() - _token_groups())
    assert not missing, (
        f"agent/mcp_server_* directories with no entry in {INTERNAL} "
        f"_TOKEN_GROUPS: {missing}\n"
        "agent/install.sh mints one HMAC token per discovered category and the "
        "bridge only accepts tokens for groups in this list, so this server's "
        "tools would present a token the bridge rejects. Add the group here and "
        "to INTERNAL_SCOPE_GROUPS, or to neither."
    )


def test_every_shipped_server_has_a_scope_entry() -> None:
    missing = sorted(_discovered_groups() - _scope_groups())
    assert not missing, (
        f"agent/mcp_server_* directories with no entry in {SECURITY} "
        f"INTERNAL_SCOPE_GROUPS: {missing}\n"
        "internal.group_may() falls back to frozenset() for an unlisted group, so "
        "EVERY /internal route is refused for this server's tools — which presents "
        "as tools that silently do nothing, not as an error. Give the group the "
        "narrowest capability set its tools actually call."
    )


def test_every_token_group_can_be_scoped() -> None:
    missing = sorted(_token_groups() - _scope_groups())
    assert not missing, (
        f"group(s) in {INTERNAL} _TOKEN_GROUPS with no {SECURITY} "
        f"INTERNAL_SCOPE_GROUPS entry: {missing}\n"
        "A token the bridge issues but the scope gate cannot classify reaches "
        "nothing. The two tables are the two halves of one decision — a group "
        "belongs in both or in neither."
    )


def test_declared_groups_without_a_server_are_the_known_ones() -> None:
    declared = _token_groups() | _scope_groups()
    stray = declared - _discovered_groups()
    unexpected = sorted(stray - _DECLARED_WITHOUT_SERVER)
    stale = sorted(_DECLARED_WITHOUT_SERVER - stray)
    assert not unexpected, (
        f"group(s) declared in {INTERNAL} / {SECURITY} with no "
        f"agent/mcp_server_<group>/ directory: {unexpected}\n"
        "install.sh discovers groups from those directories, so it never mints a "
        "token for these — the declaration is a claim the tree does not keep. "
        "Ship the server, drop the group from both tables, or record it in "
        "_DECLARED_WITHOUT_SERVER in this file with the reason."
    )
    assert not stale, (
        f"_DECLARED_WITHOUT_SERVER in this file still lists {stale}, but "
        "agent/mcp_server_<group>/ now exists for them. Remove the entry — an "
        "allowance that outlives its reason teaches people to skip allowances."
    )
