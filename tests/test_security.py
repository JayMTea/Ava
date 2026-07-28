import unittest

from ava_bridge import internal, security
from ava_bridge.config_mgmt import ConfigManager
from ava_bridge.policy_mgmt import PolicyManager


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


class ConfigMutationPolicyTests(unittest.TestCase):
    def test_allowlisted_env_key_is_mutable(self):
        self.assertIsNone(ConfigManager._validate_env_updates({"AVA_SESSION_TTL_DAYS": "14"}))

    def test_sensitive_or_unknown_env_keys_are_blocked(self):
        # secret-looking key -> refused as protected
        self.assertIn("protected", ConfigManager._validate_env_updates({"AVA_INTERNAL_TOKEN": "x"}))
        # unknown key -> deny-by-default (not on the allowlist)
        self.assertIn("allowlisted", ConfigManager._validate_env_updates({"MYAPP_BASE": "http://127.0.0.1:8097"}))
        # an explicitly allowlisted key IS mutable
        self.assertIsNone(ConfigManager._validate_env_updates({"AVA_COOKIE_SECURE": "0"}))

    def test_env_values_cannot_inject_new_lines(self):
        err = ConfigManager._validate_env_updates({"LOG_LEVEL": "INFO\nAVA_PASSWORD=oops"})
        self.assertIn("newlines", err)


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
    def test_system_policy_names_are_normalized_and_reserved(self):
        self.assertEqual(PolicyManager._normalize_name(" Ava-Knowledge "), "ava-knowledge")
        self.assertTrue(PolicyManager._reserved_name("ava-knowledge"))
        self.assertFalse(PolicyManager._reserved_name("my-connector-app"))


if __name__ == "__main__":
    unittest.main()