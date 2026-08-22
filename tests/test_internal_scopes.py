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

Style matches tests/test_diagram_sync.py: a `git ls-files` scan, no bridge, no
network.
"""
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-scopes-test-"))

from ava_bridge import internal, security

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
    def test_the_web_group_holds_no_control_plane_capability(self):
        forbidden = {"config", "policies", "logs", "perf"}
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


class TwoGatesMustNotContradictTests(unittest.TestCase):
    """A per-handler `authorized(request, scope=...)` is a SECOND gate, and the
    two vocabularies look alike enough to hide a contradiction: ROUTE_SCOPES maps
    a path to a *capability*, while `scope=` names a *group*. `content` is both a
    group name and a plausible capability name, which is how this shipped:

        /internal/devices/events   ROUTE_SCOPES -> "connectors"
                                   handler      -> scope="content"

    auth_gate admitted the `connectors` token (its server holds device_events.mjs)
    and the handler then 401'd it; the `content` token was refused 403 upstream
    before reaching the handler. No derived token could call the route at all, so
    the documented `device_events` tool was dead — while both gates individually
    looked correct.

    AST, not a text scan: `scope="content"` also appears in the prose above and in
    ROUTE_SCOPES' own comment block. Seven guards in this repo have matched their
    own explanation; this one reads the call node.
    """

    def _handlers_with_a_scope(self):
        """[(module, route, group)] for every /internal handler passing scope=."""
        import ast
        found = []
        for rel in _tracked("*.py"):
            if rel.startswith(("tests/", "qa/")):
                continue
            src = _read(rel)
            if "/internal/" not in src:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                routes = [
                    d.args[0].value
                    for d in node.decorator_list
                    if isinstance(d, ast.Call) and d.args
                    and isinstance(d.args[0], ast.Constant)
                    and isinstance(d.args[0].value, str)
                    and d.args[0].value.startswith("/internal/")
                ]
                if not routes:
                    continue
                for call in ast.walk(node):
                    if not (isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Name)
                            and call.func.id == "authorized"):
                        continue
                    for kw in call.keywords:
                        if kw.arg == "scope" and isinstance(kw.value, ast.Constant):
                            for route in routes:
                                found.append((rel, route, kw.value.value))
        return found

    def test_a_handler_scope_never_contradicts_ROUTE_SCOPES(self):
        offenders = [
            (rel, route, group)
            for rel, route, group in self._handlers_with_a_scope()
            if not internal.group_may(group, route)
        ]
        self.assertFalse(offenders, (
            "these handlers gate on a group the central table does not let reach "
            f"the route, so NO derived token can call them: {offenders}. Either "
            "drop the per-handler scope= (enforcement is central, in "
            "auth.auth_gate -> internal.group_may) or fix ROUTE_SCOPES. Remember "
            "scope= names a GROUP and ROUTE_SCOPES holds a CAPABILITY."))

    def test_the_device_events_route_is_reachable_by_its_own_server(self):
        """The behavioural half. The guard above compares two tables; this asserts
        the tool's actual token reaches the route, which is what was broken."""
        route = "/internal/devices/events"
        self.assertTrue(internal.group_may("connectors", route), (
            "device_events.mjs lives in agent/mcp_server_connectors/, so it "
            "presents the `connectors` group token. If this fails the tool 403s."))
        self.assertFalse(internal.group_may("content", route), (
            "the `content` group runs web_fetch — the surface prompt injection "
            "arrives on. It must not reach device actuation or ingest."))


if __name__ == "__main__":
    unittest.main()
