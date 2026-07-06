import unittest

from ava_bridge import security
from ava_bridge.config_mgmt import ConfigManager
from ava_bridge.policy_mgmt import PolicyManager


class InternalTokenScopeTests(unittest.TestCase):
    def test_scoped_token_allows_only_covered_scopes(self):
        base = "root-secret"
        content = security.scoped_internal_token(base, "content")

        self.assertTrue(security.token_allows_scope(content, base, "documents"))
        self.assertTrue(security.token_allows_scope(content, base, "web"))
        self.assertFalse(security.token_allows_scope(content, base, "code_change"))
        self.assertFalse(security.token_allows_scope(content, base, "config"))

    def test_raw_base_token_is_not_accepted_by_default(self):
        base = "root-secret"

        self.assertFalse(security.token_allows_scope(base, base, "documents"))


class ConfigMutationPolicyTests(unittest.TestCase):
    def test_allowlisted_env_key_is_mutable(self):
        self.assertIsNone(ConfigManager._validate_env_updates({"AVA_SESSION_TTL_DAYS": "14"}))

    def test_sensitive_or_unknown_env_keys_are_blocked(self):
        self.assertIn("protected", ConfigManager._validate_env_updates({"AVA_INTERNAL_TOKEN": "x"}))
        self.assertIn("protected", ConfigManager._validate_env_updates({"STUDIO_BASE": "http://127.0.0.1:8097"}))
        self.assertIn("allowlisted", ConfigManager._validate_env_updates({"AVA_COOKIE_SECURE": "0"}))

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