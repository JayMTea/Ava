"""Every /internal route must be classified, and the docs must not overclaim.

`internal.authorized(scope=)` was documented as enforcing least privilege in
three places — SECURITY.md, agent/install.sh, and ava_bridge/auth.py — while 24
of its 25 call sites passed no scope at all. `ava_bridge/security.py` even
carried a complete second implementation (`token_allows_scope`) with zero
non-test callers, and `tests/test_security.py` exercised that copy. So the
control was written down, implemented, tested, and enforced nowhere: any valid
group token reached every route, including /internal/code-change from `content`
— the group whose MCP server runs web_fetch, which is where prompt injection
actually arrives.

Enforcement now lives in the middleware (ava_bridge/auth.auth_gate ->
internal.group_may), so a new route is covered without its author opting in. That
only holds while every route is classified: an unclassified path is refused for
derived tokens, which is safe but presents as a broken tool. This guard turns
that into a build failure with instructions instead.

Style matches tests/test_no_eval_data.py: a `git ls-files` scan, no bridge, no
network.
"""
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-scopes-test-"))

from ava_bridge import internal, security  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]

# A FastAPI route decorator naming an /internal path.
_ROUTE = re.compile(r'@\w+\.(?:get|post|put|patch|delete|api_route)\(\s*[fr]?["\'](/internal/[^"\'{]*)')


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", pattern],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def _read(rel: str) -> str:
    try:
        return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _declared_routes() -> set[str]:
    found = set()
    for rel in _tracked("*.py"):
        if rel.startswith(("tests/", "qa/")):
            continue
        for path in _ROUTE.findall(_read(rel)):
            found.add(path.rstrip("/") or "/internal")
    return found


class RouteCoverageTests(unittest.TestCase):
    def test_every_declared_internal_route_has_a_scope(self):
        routes = _declared_routes()
        self.assertTrue(routes, "no /internal routes found — did the surface move?")
        offenders = sorted(r for r in routes if internal.required_scope(r) is None)
        self.assertFalse(offenders, (
            "these /internal routes have no entry in internal.ROUTE_SCOPES, so "
            "only the root token can call them — a sandboxed tool hitting one "
            "gets a 403 that looks like a bug. Add `\"<prefix>\": \"<capability>\"` "
            "to ROUTE_SCOPES and grant that capability to the calling group in "
            f"security.INTERNAL_SCOPE_GROUPS: {offenders}"))

    def test_every_capability_is_granted_to_at_least_one_group(self):
        granted = set()
        for caps in security.INTERNAL_SCOPE_GROUPS.values():
            granted |= set(caps)
        used = set(internal.ROUTE_SCOPES.values())
        orphans = sorted(used - granted)
        self.assertFalse(orphans, (
            "these capabilities gate a route but no group holds them, so the "
            "route is root-only by accident rather than by decision: "
            f"{orphans}"))


class LeastPrivilegeTests(unittest.TestCase):
    def test_only_admin_may_change_code(self):
        """The escalation this whole surface exists to prevent."""
        for group in security.INTERNAL_SCOPE_GROUPS:
            expected = group == "admin"
            self.assertEqual(internal.group_may(group, "/internal/code-change"),
                             expected, group)

    def test_the_web_group_holds_no_control_plane_capability(self):
        forbidden = {"code_change", "config", "policies", "logs", "perf"}
        held = set(security.INTERNAL_SCOPE_GROUPS.get("content", frozenset()))
        self.assertFalse(held & forbidden, (
            "the `content` group's server runs web_fetch, so it is the surface "
            "prompt injection arrives on; it must hold no control-plane "
            f"capability: {sorted(held & forbidden)}"))

    def test_no_group_holds_every_capability(self):
        """A group that holds everything is the root token with extra steps."""
        every = set(internal.ROUTE_SCOPES.values())
        for group, caps in security.INTERNAL_SCOPE_GROUPS.items():
            self.assertNotEqual(set(caps), every, f"{group} is root in disguise")


class DocsMatchTheCodeTests(unittest.TestCase):
    def test_the_removed_duplicate_is_really_gone(self):
        """security.token_allows_scope / scoped_internal_token were a parallel
        implementation with no non-test callers. Keeping two answers to "may this
        token do this" is how one of them stopped being true."""
        src = _read("ava_bridge/security.py")
        for name in ("def token_allows_scope", "def scoped_internal_token"):
            self.assertNotIn(name, src, (
                f"{name} is back in security.py. Token derivation belongs in "
                "ava_bridge/internal.py (_derived_token); this module keeps the "
                "INTERNAL_SCOPE_GROUPS data table only."))

    def test_the_gate_is_wired_into_the_middleware(self):
        src = _read("ava_bridge/auth.py")
        self.assertIn("internal.group_may", src, (
            "auth.auth_gate must call internal.group_may() for /internal/*. "
            "Per-handler checks were the previous design and 24 of 25 handlers "
            "forgot to pass a scope."))


if __name__ == "__main__":
    unittest.main()
