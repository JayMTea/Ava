"""Cross-app links preserve their selection without carrying embed credentials."""
import unittest
from unittest.mock import patch

from starlette.requests import Request

from ava_bridge import apps_origin, auth


class AppDeepLinkTests(unittest.TestCase):
    def test_selected_model_survives_the_shell_redirect(self):
        request = Request({
            "type": "http", "method": "GET", "scheme": "https",
            "server": ("apps.example", 443),
            "path": "/apps/infra/machine-learning",
            "query_string": b"model=approved-model&t=spent-token&entity=A%26B",
            "headers": [(b"sec-fetch-dest", b"document")],
        })
        with patch.object(apps_origin, "configured", return_value="https://apps.example"), \
             patch.object(auth.config, "PUBLIC_URL", "https://ava.example"):
            response = auth._shell_bounce(request, request.url.path)
        self.assertEqual(response.headers["location"],
                         "https://ava.example/#infra/machine-learning?model=approved-model&entity=A%26B")
        self.assertNotIn("spent-token", response.headers["location"])
