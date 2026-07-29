"""Every scaffolded agent surface must refuse an uncredentialed call.

`ava app new` writes code into a FORKER's own repo, on their own port. Ava's
just-in-time consent tiers live on Ava's side of the wire and enforce nothing
there — so an unauthenticated `/call` hands every registered tool, including
anything tiered `destructive`, to whoever can reach the port. When the surface
is mounted into an app that already listens on a LAN, that is everyone on it.

The templates shipped without any credential check, with `token_env` commented
out in the generated manifest and a README that never mentioned authenticating.
This test is the ratchet: a template that loses its guard fails here rather than
in someone else's deployment.
"""
import re
import unittest

from ava_bridge import scaffold

FRAMEWORKS = ("fastapi", "flask", "stdlib", "express")

# Routes that execute app code, and the one that must stay open so Ava's
# Detect can prefill the connect form before a token exists.
_GUARDED = ("/tools", "/call")
_OPEN = "/.well-known/ava.json"


def _surface(framework: str) -> str:
    out = scaffold._render("demo", "Demo", framework, 9400, False)
    return next(v for k, v in out.items() if "surface" in k)


class ScaffoldAuthTests(unittest.TestCase):
    def test_every_template_checks_a_bearer_token(self):
        for fw in FRAMEWORKS:
            with self.subTest(framework=fw):
                src = _surface(fw)
                self.assertRegex(
                    src, r"_authorized|authorized\(req\)",
                    f"the {fw} surface has no credential check — /call executes "
                    "app code for anyone who can reach the port")
                self.assertIn("DEMO_TOKEN", src,
                              f"the {fw} surface never names its token env var")

    def test_every_template_fails_closed(self):
        """An unset token must refuse, never wave calls through."""
        for fw in FRAMEWORKS:
            with self.subTest(framework=fw):
                src = _surface(fw)
                self.assertRegex(
                    src, r"if not want|if \(!want\)",
                    f"the {fw} surface does not fail closed on a missing token")
                self.assertIn("401", src, f"the {fw} surface never returns 401")

    def test_detect_endpoint_stays_unauthenticated(self):
        """Guarding the well-known document would break Connect-an-app: Detect
        reads it before the owner has pasted a credential anywhere."""
        for fw in FRAMEWORKS:
            with self.subTest(framework=fw):
                src = _surface(fw)
                idx = src.find(_OPEN)
                self.assertGreater(idx, -1, f"{fw}: no well-known route")
                # The handler body directly after the route registration must
                # not contain a guard.
                window = src[idx:idx + 400]
                self.assertNotRegex(
                    window.split("/tools")[0], r"_authorized|authorized\(req\)",
                    f"{fw}: the well-known document is gated, which breaks Detect")

    def test_manifest_declares_the_token(self):
        for fw in FRAMEWORKS:
            with self.subTest(framework=fw):
                man = scaffold._render("demo", "Demo", fw, 9400, False)["ava/connector.yaml"]
                self.assertRegex(
                    man, re.compile(r"^\s{4}token_env: \w+", re.M),
                    "connector.yaml must declare token_env uncommented — a "
                    "guarded surface with an undeclared token is an app Ava "
                    "cannot call at all")

    def test_quickstart_hands_over_a_real_token(self):
        """'Generate a token' is the step people skip, and the surface fails
        closed without one — so the README must contain a working export."""
        for fw in FRAMEWORKS:
            with self.subTest(framework=fw):
                readme = scaffold._render("demo", "Demo", fw, 9400, False)["ava/README-AVA.md"]
                m = re.search(r"export DEMO_TOKEN=(\S+)", readme)
                self.assertIsNotNone(m, f"{fw}: no export line in the quickstart")
                self.assertGreaterEqual(len(m.group(1)), 32,
                                        f"{fw}: quickstart token is too short to be real")

    def test_tokens_are_unique_per_scaffold(self):
        a = scaffold._render("demo", "Demo", "fastapi", 9400, False)["ava/README-AVA.md"]
        b = scaffold._render("demo", "Demo", "fastapi", 9400, False)["ava/README-AVA.md"]
        self.assertNotEqual(a, b, "every scaffold must mint its own token")


if __name__ == "__main__":
    unittest.main()
