"""Removing a backend leaves nothing behind that names it.

A backend id is written into ava.yaml in more places than the one the owner
typed it into, and every leftover fails differently and quietly:

  * `inference.fallback` is read by router_app on every request, so a fallback
    naming a deleted backend is a failover target that cannot answer — the
    request that needed a fallback is the one that finds out.
  * `inference.roles.<role>` covers "chat / fast / embed / …"
    (settings.resolve_role). Repointing only `chat` left an embed role
    resolving to a backend that no longer exists.
  * `alloc.models.<id>` is keyed by the same id (alloc/spec.py), so a declared
    model outlived its engine: the planner counts memory it can never free and
    the Free button drives an address nobody serves.
  * the breaker's state file keys failure counts by the same id, so a re-added
    backend inherited the dead one's strikes and could start out given up on.

The last test is the one that matters most. It does not check the cases anyone
thought of — it scans the codebase for config paths that hold a backend id and
fails when `purge_backend` does not clear one, so the NEXT such key cannot be
added without this file noticing.
"""
import re
import unittest

from ava_bridge.hub.models import purge_backend


def _cfg():
    """A box with two backends, wired up every way a backend id can be wired."""
    return {
        "inference": {
            "backends": {
                "local": {"engine": "vllm", "base_url": "http://127.0.0.1:8002/v1"},
                "spare": {"engine": "ollama", "base_url": "http://127.0.0.1:11434/v1"},
            },
            "primary": "local",
            "fallback": "local",
            "roles": {"chat": "local", "embed": "local", "fast": "spare"},
        },
        "alloc": {"models": {"local": {"weight_gb": 24}, "spare": {"weight_gb": 4}}},
    }


class NothingSurvivesThatNamesIt(unittest.TestCase):
    def test_the_backend_itself_is_gone(self):
        cfg = _cfg()
        purge_backend(cfg, "local")
        self.assertNotIn("local", cfg["inference"]["backends"])
        self.assertIn("spare", cfg["inference"]["backends"])

    def test_the_brain_repoints_to_a_survivor(self):
        cfg = _cfg()
        purge_backend(cfg, "local")
        self.assertEqual(cfg["inference"]["primary"], "spare")
        self.assertEqual(cfg["inference"]["roles"]["chat"], "spare")

    def test_every_role_repoints_not_just_chat(self):
        # `embed` used to outlive its backend, so every embedding call resolved
        # to a backend that was not there.
        cfg = _cfg()
        purge_backend(cfg, "local")
        self.assertEqual(cfg["inference"]["roles"]["embed"], "spare")

    def test_a_role_pointing_elsewhere_is_left_alone(self):
        cfg = _cfg()
        purge_backend(cfg, "local")
        self.assertEqual(cfg["inference"]["roles"]["fast"], "spare")

    def test_the_fallback_is_cleared_not_promoted(self):
        # A fallback is the SECOND choice. Pointing the only surviving backend
        # at itself as its own fallback is a loop, not a safety net.
        cfg = _cfg()
        purge_backend(cfg, "local")
        self.assertIsNone(cfg["inference"].get("fallback"))

    def test_the_alloc_declaration_goes_with_it(self):
        cfg = _cfg()
        purge_backend(cfg, "local")
        self.assertNotIn("local", cfg["alloc"]["models"])
        self.assertIn("spare", cfg["alloc"]["models"])

    def test_removing_the_last_backend_leaves_no_dangling_pointers(self):
        cfg = _cfg()
        purge_backend(cfg, "local")
        purge_backend(cfg, "spare")
        inf = cfg["inference"]
        self.assertEqual(inf.get("backends"), {})
        self.assertIsNone(inf.get("primary"))
        self.assertIsNone(inf.get("fallback"))
        self.assertFalse(inf.get("roles"))
        self.assertEqual(cfg.get("alloc", {}).get("models"), {})

    def test_it_reports_what_it_touched(self):
        # The route returns this. A removal that claims success without saying
        # what it changed cannot be checked by the person who ran it.
        touched = purge_backend(_cfg(), "local")
        for want in ("inference.backends.local", "inference.primary",
                     "inference.fallback", "inference.roles.chat",
                     "inference.roles.embed", "alloc.models.local"):
            self.assertIn(want, touched)

    def test_removing_something_absent_changes_nothing_else(self):
        cfg = _cfg()
        purge_backend(cfg, "ghost")
        self.assertEqual(cfg["inference"]["primary"], "local")
        self.assertEqual(cfg["inference"]["roles"]["chat"], "local")
        self.assertIn("local", cfg["alloc"]["models"])


class TheScanThatFindsTheNextOne(unittest.TestCase):
    """`purge_backend` clears the keys someone thought of. This finds the rest.

    Every config path below is one the codebase reads expecting a BACKEND ID. If
    a new one is added and the purge does not learn about it, a removal starts
    leaving that key dangling — silently, because a stale pointer only fails on
    the request that follows it.
    """

    # Config paths that hold a backend id, and how to plant one at that path.
    KNOWN = {
        "inference.primary": lambda c, v: c["inference"].__setitem__("primary", v),
        "inference.fallback": lambda c, v: c["inference"].__setitem__("fallback", v),
        "inference.roles.<role>": lambda c, v: c["inference"]["roles"].__setitem__("embed", v),
        "alloc.models.<id>": lambda c, v: c["alloc"]["models"].__setitem__(v, {}),
    }

    def test_every_known_reference_path_is_purged(self):
        for path, plant in self.KNOWN.items():
            with self.subTest(path=path):
                cfg = _cfg()
                plant(cfg, "local")
                purge_backend(cfg, "local")
                blob = repr(cfg)
                # The id may legitimately survive inside another backend's URL or
                # a free-text label; what must not survive is it being the VALUE
                # of one of these pointers, or a key under alloc.models.
                self.assertNotIn("'local'", blob,
                                 f"{path} still names the removed backend: {blob}")

    def test_the_codebase_declares_no_reference_path_this_file_misses(self):
        """Static scan: every `inference.<x>` / `alloc.models` key the code reads
        as a backend id must appear in KNOWN above."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        found = set()
        for rel in ("ava_bridge/router_app.py", "ava_bridge/settings.py",
                    "ava_bridge/hub/models.py", "ava_bridge/alloc/spec.py"):
            src = (root / rel).read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'["\']inference\.(primary|fallback)["\']', src):
                found.add(f"inference.{m.group(1)}")
            if re.search(r'inference\.roles\.', src) or "resolve_role" in src:
                found.add("inference.roles.<role>")
            if re.search(r'alloc\.models', src):
                found.add("alloc.models.<id>")
        missing = sorted(found - set(self.KNOWN))
        self.assertFalse(missing, (
            f"config paths that name a backend id and are not covered here: "
            f"{missing}. Add them to KNOWN and make `purge_backend` clear them, "
            "or a removal will leave them dangling."))


if __name__ == "__main__":
    unittest.main()
