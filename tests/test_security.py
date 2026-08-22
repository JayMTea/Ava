import importlib.util
import pathlib
import unittest

from ava_bridge import config, config_mgmt, internal, policy_mgmt, security
from ava_bridge.config_mgmt import ConfigManager
from ava_bridge.policy_mgmt import PolicyManager

ROOT = pathlib.Path(__file__).resolve().parents[1]


class InternalTokenScopeTests(unittest.TestCase):
    """Asserted against the LIVE gate (internal.group_may, called by
    auth.auth_gate) rather than the parallel implementation that used to live in
    security.py. That second copy had zero non-test callers, so these two tests
    passed for months while every /internal route in the running app accepted
    every group token — the control was tested and not enforced."""

    def test_a_group_reaches_only_its_own_capabilities(self):
        self.assertTrue(internal.group_may("content", "/internal/documents"))
        self.assertTrue(internal.group_may("content", "/internal/web/fetch"))
        self.assertFalse(internal.group_may("content", "/internal/code-change"))
        self.assertFalse(internal.group_may("content", "/internal/config"))
        self.assertFalse(internal.group_may("content", "/internal/policies"))

    def test_the_web_fetching_group_can_never_edit_source(self):
        """`content` is the group whose MCP server runs web_fetch, so it is where
        prompt injection actually arrives. It reaching /internal/code-change is
        injection -> arbitrary governed edits to Ava's own code."""
        for path in ("/internal/code-change", "/internal/config",
                     "/internal/policies/ava-code-changes", "/internal/logs"):
            self.assertFalse(internal.group_may("content", path), path)

    def test_the_root_token_still_passes_everywhere(self):
        for path in ("/internal/code-change", "/internal/documents",
                     "/internal/anything-unclassified"):
            self.assertTrue(internal.group_may("root", path), path)

    def test_an_unclassified_route_fails_closed(self):
        """Forgetting to classify a new route must not leave it open to every
        group token, which is exactly how this surface behaved before."""
        self.assertIsNone(internal.required_scope("/internal/brand-new-route"))
        for group in security.INTERNAL_SCOPE_GROUPS:
            self.assertFalse(internal.group_may(group, "/internal/brand-new-route"),
                             group)

    def test_the_longest_matching_prefix_wins(self):
        self.assertEqual(internal.required_scope("/internal/learning/state"), "learning")
        self.assertEqual(internal.required_scope("/internal/web/search"), "web")
        self.assertEqual(internal.required_scope("/internal/connector/x/act"), "connectors")


class ConfigMutationIsRemovedTests(unittest.TestCase):
    """`config_mgmt` is read-only. There is no way for the agent to write config.

    There was one: `ConfigManager.update_config`, reachable from the sandbox via
    `update_config.mjs` -> `POST /internal/config`. Its `CONFIG_PATHS` reached
    `.env`, `ava_learning_digest.py` (executable Python) and
    `agent/persona.txt.tmpl` — the agent's own system prompt, and precisely the
    file the code-change policy of the day placed behind owner approval. It wrote
    all three with no diff, no commit and no review, so the two enforcement
    layers gave opposite answers about one asset.

    The env half was allowlisted and defensible; the other two were not, and a
    write path kept for one component is a write path. It went with self-editing.
    Restoring it is a deliberate feature decision, so the absence is pinned here.
    """

    def test_config_mgmt_exposes_no_write_verb(self):
        for verb in ("update_config", "write_config", "save_config",
                     "_validate_env_updates"):
            self.assertFalse(hasattr(config_mgmt, verb),
                             f"config_mgmt.{verb} is back")
            self.assertFalse(hasattr(ConfigManager, verb),
                             f"ConfigManager.{verb} is back")

    def test_only_env_is_reachable_at_all(self):
        """The persona template and the digest script were the dangerous two."""
        self.assertEqual(set(ConfigManager.CONFIG_PATHS), {"env"})

    def test_no_internal_route_writes_config(self):
        routes = {(m, r.path)
                  for r in internal.router.routes
                  for m in getattr(r, "methods", set() or set())}
        self.assertIn(("GET", "/internal/config"), routes,
                      "reading config is how Ava explains her own setup")
        self.assertNotIn(("POST", "/internal/config"), routes)

    def test_no_agent_tool_still_calls_it(self):
        offenders = [p.name for p in (ROOT / "agent").rglob("*.mjs")
                     if "postJson" in p.read_text(encoding="utf-8")
                     and "/internal/config" in p.read_text(encoding="utf-8")]
        self.assertFalse(offenders,
                         f"these tools POST to the removed route: {offenders}")


class SelfEditingIsRemovedTests(unittest.TestCase):
    """Ava cannot edit source code, and cannot be re-granted the ability by accident.

    She could. `code_change_request` (the `admin` MCP server) handed an
    engineering task to Claude over `POST /internal/code-change`; `code_agent.py`
    then classified each edit against a glob policy in `access_policy.py` and,
    for anything not on the approval list, wrote the file, `git commit`ed it as
    `Ava <ava@localhost>` and `systemctl --user restart`ed the bridge so the
    change took effect. `coder.py` drove the Claude tool loop behind both.

    That capability was removed in full rather than tightened, and the whole of
    it is pinned here rather than just the route. The reason is that it was never
    one thing: it was a skill that taught the model when to ask, a tool that
    asked, an egress policy that let the ask leave the sandbox, a scope that let
    it through the gate, a route that accepted it, three modules that carried it
    out, and an API key that paid for it. Restoring any single layer would be a
    partial re-arming — a route with no scope, a tool with no policy — which
    reads as a bug rather than as a decision. Each assertion below is a different
    layer, so no single edit quietly puts it back.

    Same shape, and the same reasoning, as PolicyMutationTests above.
    """

    def test_the_engine_modules_are_gone(self):
        for mod in ("ava_bridge.code_agent", "ava_bridge.coder",
                    "ava_bridge.access_policy"):
            self.assertIsNone(importlib.util.find_spec(mod), f"{mod} is back")

    def test_the_learning_modules_are_gone(self):
        """Learning parked the proposals and recommended further edits."""
        for mod in ("ava_bridge.learning", "ava_bridge.learning_api",
                    "ava_bridge.learning_mgmt"):
            self.assertIsNone(importlib.util.find_spec(mod), f"{mod} is back")

    def test_no_route_accepts_a_code_change(self):
        routes = {(m, r.path)
                  for r in internal.router.routes
                  for m in getattr(r, "methods", set() or set())}
        self.assertNotIn(("POST", "/internal/code-change"), routes)

    def test_the_scope_is_gone_from_both_tables(self):
        self.assertNotIn("code_change", set(internal.ROUTE_SCOPES.values()))
        for group, caps in security.INTERNAL_SCOPE_GROUPS.items():
            self.assertNotIn("code_change", caps, group)

    def test_no_group_can_reach_the_route(self):
        """The inverse of the deleted test_only_admin_may_change_code: an
        unclassified path fails closed for every derived token."""
        for group in security.INTERNAL_SCOPE_GROUPS:
            self.assertFalse(internal.group_may(group, "/internal/code-change"),
                             group)

    def test_no_agent_tool_asks_for_one(self):
        offenders = [p.name for p in (ROOT / "agent").rglob("*.mjs")
                     if "/internal/code-change" in p.read_text(encoding="utf-8")]
        self.assertFalse(offenders, f"these tools call the removed route: {offenders}")

    def test_no_egress_policy_grants_it(self):
        """The sandbox is deny-by-default, so a policy is the other half of the
        tool. A grant for a route that does not exist is a lie in the one file
        whose job is to describe what Ava may reach."""
        for pol in (ROOT / "agent" / "policies").rglob("*.yaml"):
            body = pol.read_text(encoding="utf-8")
            self.assertNotIn("/internal/code-change", body, pol.name)
            self.assertNotIn("api.anthropic.com", body, pol.name)

    def test_no_skill_teaches_it(self):
        self.assertFalse((ROOT / "agent" / "skills" / "ava-self-coding").exists())

    def test_the_third_party_model_key_is_gone(self):
        """This is what keeps "Ava is fully local" true. The key powered the
        code agent; the learning cloud fallback was its only other consumer."""
        for attr in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE", "CODE_MODEL",
                     "CODE_APPROVAL", "PROJECTS"):
            self.assertFalse(hasattr(config, attr), f"config.{attr} is back")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("ANTHROPIC_API_KEY=", env_example)

    def test_the_approval_gate_went_with_the_thing_it_gated(self):
        """`code.approval` chose between auto-commit and an approval queue. With
        nothing to gate, leaving the knob would advertise a capability that is
        not there."""
        template = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        self.assertNotIn("approval:", template)


class ArchitectureIsReadOnlyTests(unittest.TestCase):
    """Ava reports architecture drift; she does not commit a fix for it.

    `update_architecture` and `sync_diagrams` regenerated the manifest and the
    rendered diagrams and auto-committed the result, and `arch_watch` did the
    same on a timer, authored "Ava (auto-sync)", onto whatever branch the working
    tree happened to be on. That is an agent committing to the repo, which is the
    thing self-editing's removal is about — so the write half went and the report
    stayed. Reconcile with `python agent/docs/arch.py sync`, by hand.
    """

    def test_no_route_writes_the_manifest_or_the_diagrams(self):
        routes = {(m, r.path)
                  for r in internal.router.routes
                  for m in getattr(r, "methods", set() or set())}
        self.assertNotIn(("POST", "/internal/architecture/update"), routes)
        self.assertNotIn(("POST", "/internal/architecture/sync"), routes)
        self.assertIn(("POST", "/internal/architecture/check"), routes,
                      "drift REPORTING is the half that is deliberately kept")

    def test_the_write_tools_are_gone(self):
        arch_tools = ROOT / "agent" / "mcp_server_system" / "architecture"
        for gone in ("update_architecture.mjs", "sync_diagrams.mjs"):
            self.assertFalse((arch_tools / gone).exists(), gone)
        self.assertTrue((arch_tools / "check_drift.mjs").exists(),
                        "reporting drift is what the agent keeps")

    def test_the_watchdog_cannot_commit(self):
        """Reads the CODE, not the prose. The module docstring explains what the
        self-heal used to do and why it went — that history is the point of the
        comment, and a substring scan over the raw file would forbid writing it
        down."""
        import ast
        src = (ROOT / "ava_bridge" / "arch_watch.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        literals = {n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        docstrings = {ast.get_docstring(n) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))}
        self.assertNotIn("--commit", literals - docstrings,
                         "the auto-commit self-heal is back")
        self.assertIn("push_external", ast.unparse(tree),
                      "drift reporting is the half this watchdog exists for")


class ProxyPolicyTests(unittest.TestCase):
    def test_local_http_origin_accepts_only_loopback_and_allowed_port(self):
        self.assertEqual(
            security.local_http_origin("http://127.0.0.1:8097", allowed_ports={8097}),
            "http://127.0.0.1:8097",
        )
        with self.assertRaises(ValueError):
            security.local_http_origin("http://0.0.0.0:8097", allowed_ports={8097})
        with self.assertRaises(ValueError):
            security.local_http_origin("http://127.0.0.1:8000", allowed_ports={8097})

    def test_allowed_proxy_path_blocks_traversal_and_unknown_prefixes(self):
        prefixes = ("api/persona/", "api/personas", "media/")

        self.assertTrue(security.allowed_proxy_path("api/persona/april", prefixes))
        self.assertTrue(security.allowed_proxy_path("/media/thumb.png", prefixes))
        self.assertFalse(security.allowed_proxy_path("api/secret", prefixes))
        self.assertFalse(security.allowed_proxy_path("api/persona/../secret", prefixes))


class PolicyMutationTests(unittest.TestCase):
    """There is no way for the agent to rewrite its own egress boundary.

    One existed: `POST /internal/policies` -> `policy_mgmt.update_policy`, callable
    from the sandbox by the `admin` group. It had never worked — it validated a
    `{name, rules}` shape against a corpus that is entirely
    `{preset, network_policies}` — and it contradicted `access_policy.py`, which
    puts `agent/policies/**` in the OWNER-APPROVAL tier. Two enforcement layers
    with opposite answers about one asset, held apart only by a bug.

    Restoring it is a deliberate feature decision (design it against the
    approvals gate), never an incidental one, so the absence is pinned here.
    """

    def test_policy_mgmt_exposes_no_write_verb(self):
        for verb in ("update_policy", "create_policy", "delete_policy",
                     "write_policy"):
            self.assertFalse(hasattr(policy_mgmt, verb),
                             f"policy_mgmt.{verb} is back")
            self.assertFalse(hasattr(PolicyManager, verb),
                             f"PolicyManager.{verb} is back")

    def test_no_internal_route_writes_a_policy(self):
        routes = {(m, r.path)
                  for r in internal.router.routes
                  for m in getattr(r, "methods", set() or set())}
        self.assertIn(("GET", "/internal/policies"), routes,
                      "reading policies is how Ava explains her own limits")
        self.assertNotIn(("POST", "/internal/policies"), routes,
                         "the sandboxed agent can rewrite the boundary that "
                         "contains it again")

    def test_the_sandbox_is_not_granted_a_write_it_cannot_use(self):
        """A grant for a route that no longer exists is a lie in the one file
        whose job is to describe what Ava may reach."""
        pol = (pathlib.Path(__file__).resolve().parents[1]
               / "agent" / "policies" / "ava-policies.yaml").read_text(encoding="utf-8")
        self.assertNotIn("method: POST", pol)

    def test_no_agent_tool_still_calls_it(self):
        root = (pathlib.Path(__file__).resolve().parents[1]
                / "agent" / "mcp_server_admin")
        offenders = [p.name for p in root.rglob("*.mjs")
                     if "postJson" in p.read_text(encoding="utf-8")
                     and "/internal/policies" in p.read_text(encoding="utf-8")]
        self.assertFalse(offenders,
                         f"these tools POST to the removed route: {offenders}")


class ConstantTimeCompareTests(unittest.TestCase):
    """`hmac.compare_digest` raises TypeError on a str with any non-ASCII
    character. Every secret comparison in the app used to pass str straight in,
    so one accented character was an unhandled 500 in two places that matter:
    POST /login (the owner is locked out of the only page that could change the
    password) and the /internal bearer check, where the token is supplied by the
    CALLER — making it an unauthenticated way to force a server error.

    These assert the property, not the call site, so the guarantee survives a
    refactor of either route."""

    def test_non_ascii_secrets_compare_without_raising(self):
        for secret in ("café1234", "pässwörd", "密码密码", "naïve-token-99",
                       "emoji-🔑-key"):
            with self.subTest(secret=secret):
                self.assertTrue(security.constant_time_equals(secret, secret))
                self.assertFalse(
                    security.constant_time_equals(secret, secret + "x"))

    def test_the_raw_primitive_would_have_raised(self):
        """Pins WHY the helper exists: drop it and this is the 500 you get."""
        import hmac
        with self.assertRaises(TypeError):
            hmac.compare_digest("café1234", "café1234")

    def test_a_lone_surrogate_does_not_raise_either(self):
        """Header/form decoding can yield an unpaired surrogate; a plain
        .encode("utf-8") would raise UnicodeEncodeError and reintroduce the
        same 500 through a different door."""
        self.assertFalse(security.constant_time_equals("\ud800bad", "expected"))
        self.assertTrue(security.constant_time_equals("\ud800ok", "\ud800ok"))

    def test_ascii_and_bytes_still_behave(self):
        self.assertTrue(security.constant_time_equals("abc123", "abc123"))
        self.assertFalse(security.constant_time_equals("abc123", "abc124"))
        self.assertTrue(security.constant_time_equals(b"abc123", "abc123"))
        self.assertFalse(security.constant_time_equals("", "nonempty"))

    def test_mismatched_length_is_false_not_an_error(self):
        self.assertFalse(security.constant_time_equals("é", "ééééééé"))


if __name__ == "__main__":
    unittest.main()