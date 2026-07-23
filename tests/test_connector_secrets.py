"""Connector credentials: the VALUE lives server-side (secrets/env/<NAME>, 0600),
keyed by the env-var NAME a manifest references — never in the manifest and never
in the generated agent tools. A real environment variable still wins at read
time. This is what lets a redeploy reuse a saved token without re-prompting while
the Ava-never-has-passwords invariant holds (the agent never sees the value)."""
import os
import tempfile
import unittest
from unittest import mock

from ava_bridge import connectors, settings


class EnvSecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"AVA_SECRETS_DIR": self.tmp}, clear=False)
        self._env.start()
        for k in ("MYAPP_TOKEN", "OTHER_TOKEN"):
            os.environ.pop(k, None)

    def tearDown(self):
        self._env.stop()

    def test_set_read_clear_roundtrip_0600(self):
        self.assertIsNone(settings.env_secret("MYAPP_TOKEN"))
        self.assertFalse(settings.has_env_secret("MYAPP_TOKEN"))
        settings.set_env_secret("MYAPP_TOKEN", "sk-123")
        self.assertEqual(settings.env_secret("MYAPP_TOKEN"), "sk-123")
        self.assertTrue(settings.has_env_secret("MYAPP_TOKEN"))
        self.assertTrue(settings.env_secret_stored("MYAPP_TOKEN"))
        p = os.path.join(self.tmp, "env", "MYAPP_TOKEN")
        self.assertTrue(os.path.isfile(p))
        self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600")
        settings.clear_env_secret("MYAPP_TOKEN")
        self.assertIsNone(settings.env_secret("MYAPP_TOKEN"))
        self.assertFalse(settings.env_secret_stored("MYAPP_TOKEN"))

    def test_real_env_var_wins_then_falls_back_to_store(self):
        settings.set_env_secret("MYAPP_TOKEN", "fromstore")
        with mock.patch.dict(os.environ, {"MYAPP_TOKEN": "fromenv"}):
            self.assertEqual(settings.env_secret("MYAPP_TOKEN"), "fromenv")
        # …env var gone → the saved value serves.
        self.assertEqual(settings.env_secret("MYAPP_TOKEN"), "fromstore")

    def test_empty_value_is_a_noop(self):
        settings.set_env_secret("MYAPP_TOKEN", "")
        self.assertFalse(settings.env_secret_stored("MYAPP_TOKEN"))

    def test_missing_name_is_safe(self):
        self.assertIsNone(settings.env_secret(None))
        self.assertIsNone(settings.env_secret(""))
        self.assertFalse(settings.has_env_secret(""))


class AuthEnvNameTests(unittest.TestCase):
    """auth_env() finds the credential's env-var NAME across every manifest shape."""

    def test_across_shapes(self):
        self.assertEqual(connectors.auth_env({"auth": {"token_env": "A"}}), "A")
        self.assertEqual(connectors.auth_env({"mcp": {"token_env": "B"}}), "B")
        self.assertEqual(connectors.auth_env({"actions": {"discover": {"token_env": "C"}}}), "C")
        self.assertEqual(connectors.auth_env({"ui": {"api": {"token_env": "D"}}}), "D")
        self.assertIsNone(connectors.auth_env({"label": "no auth"}))
        self.assertIsNone(connectors.auth_env("not a dict"))


class AuthHeaderFromStoreTests(unittest.TestCase):
    """The bearer header resolves from the saved secret, and the value never
    lands in the manifest or a generated tool (the load-bearing invariant)."""

    SECRET = "sk-secret-xyz-987"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.user = os.path.join(self.tmp, "user")
        self.builtin = os.path.join(self.tmp, "builtin")
        os.makedirs(self.user)
        os.makedirs(self.builtin)
        self._ps = [
            mock.patch.object(connectors, "BUILTIN_DIR", self.builtin),
            mock.patch.object(connectors, "USER_DIR", self.user),
            mock.patch.dict(os.environ, {"AVA_SECRETS_DIR": os.path.join(self.tmp, "secrets")},
                            clear=False),
        ]
        for p in self._ps:
            p.start()
        os.environ.pop("MYAPP_TOKEN", None)
        d = os.path.join(self.builtin, "myapp")
        os.makedirs(d)
        self.manifest_path = os.path.join(d, "connector.yaml")
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            f.write("id: myapp\nlabel: My App\nbase_url: http://127.0.0.1:9000\n"
                    "auth:\n  token_env: MYAPP_TOKEN\n"
                    "actions:\n  - id: ping\n    path: /ping\n    method: GET\n")
        connectors.load(force=True)

    def tearDown(self):
        for p in self._ps:
            p.stop()
        connectors.load(force=True)

    def test_header_empty_until_a_credential_exists(self):
        self.assertEqual(connectors._auth_headers("myapp"), {})

    def test_header_from_saved_secret_and_value_never_in_manifest(self):
        settings.set_env_secret("MYAPP_TOKEN", self.SECRET)
        self.assertEqual(connectors._auth_headers("myapp"),
                         {"Authorization": "Bearer " + self.SECRET})
        self.assertNotIn(self.SECRET, open(self.manifest_path, encoding="utf-8").read())

    def test_value_never_appears_in_a_generated_tool(self):
        settings.set_env_secret("MYAPP_TOKEN", self.SECRET)
        action = {"id": "ping", "path": "/ping", "method": "GET", "description": "ping"}
        src = connectors.render_tool("myapp", action)
        self.assertNotIn(self.SECRET, src)

    def test_app_token_resolves_saved_secret_for_embedded_proxy(self):
        # app_token is what the same-origin app proxy injects so an embedded app
        # the owner already connected never shows its own login. '' until saved.
        self.assertEqual(connectors.app_token("myapp"), "")
        settings.set_env_secret("MYAPP_TOKEN", self.SECRET)
        self.assertEqual(connectors.app_token("myapp"), self.SECRET)

    def test_app_token_empty_for_unknown_or_authless_connector(self):
        self.assertEqual(connectors.app_token("nope"), "")  # no such connector
        # A connector that declares no token_env yields '' (no auth to present).
        d = os.path.join(self.builtin, "plain")
        os.makedirs(d)
        with open(os.path.join(d, "connector.yaml"), "w", encoding="utf-8") as f:
            f.write("id: plain\nlabel: Plain\nui:\n  embed: iframe\n"
                    "  url: http://127.0.0.1:9100\n")
        connectors.load(force=True)
        self.assertEqual(connectors.app_token("plain"), "")


if __name__ == "__main__":
    unittest.main()
