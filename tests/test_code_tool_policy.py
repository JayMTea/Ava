"""The code agent may not READ what it may not write.

`access_policy._DENY` is precisely the set of secrets, credentials and
biometrics. Until this landed it was consulted only by `classify_edits` — the
WRITE path — so every one of these worked:

    read_file('.env')                -> 1739 bytes, including ANTHROPIC_API_KEY
    read_file('data/auth_password')  -> the admin password, in clear
    list_dir('secrets')              -> an inventory of the secrets directory
    search('ANTHROPIC_API_KEY')      -> the key itself, inline from .env

`.env` sits at the repo root, in no skipped directory, and matched like any
other text file. With `web_fetch` in the same tool loop that is a complete
exfiltration path, and any prompt injection reaching the agent — a fetched page,
an ingested email, an uploaded document — could walk it. `code.approval` does not
help: it gates writes, after the loop has already read and quoted.

The gate lives in `coder._safe()` because every tool must resolve a path through
it to have a legal path at all, so a tool written tomorrow inherits it. Two
channels do their own filesystem walk and cannot rely on that — `_tool_search`
(os.walk + open) and `_tool_list_dir` (os.listdir) — so they restate it, and the
last test here is what stops a third such channel appearing unnoticed.
"""
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-codepolicy-test-"))

from ava_bridge import access_policy, coder  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Paths whose CONTENT must never reach the model.
_SECRETS = [
    ".env",
    "data/auth_password",
    "data/.secret",
    "data/.internal_token",
    "data/setup_claim",
    "secrets/router_token",
    "secrets/inference_key",
    "secrets/env/SOME_APP_TOKEN",
    "ava.yaml",
    "connector_grants.yaml",
    ".venv/lib/python3.12/site-packages/anything.py",
]


class ReadGateTests(unittest.TestCase):
    def test_reading_a_denied_path_is_refused(self):
        for rel in _SECRETS:
            with self.assertRaises(PermissionError, msg=rel):
                coder._tool_read_file(rel, {})

    def test_the_refusal_names_the_path(self):
        """This string reaches the model AND the user. 'permission denied' with
        no path reads as a bug rather than a rule, and the model retries."""
        try:
            coder._tool_read_file(".env", {})
        except PermissionError as e:
            self.assertIn(".env", str(e))
            self.assertIn("access policy", str(e))
        else:
            self.fail("reading .env was not refused")

    def test_listing_a_denied_directory_is_refused(self):
        with self.assertRaises(PermissionError):
            coder._tool_list_dir("secrets")

    def test_a_root_listing_hides_denied_entries(self):
        out = coder._tool_list_dir("")
        for name in (".env", "ava.yaml", "secrets", "secrets/"):
            self.assertNotIn(name, out.splitlines(), name)

    def test_ordinary_source_is_still_readable(self):
        """The gate is worthless if it also blocks the agent's actual job."""
        body = coder._tool_read_file("ava_bridge/version.py", {})
        self.assertIn("version", body.lower())
        self.assertNotIn("blocked by access policy", body)

    def test_staged_writes_to_denied_paths_are_refused_earlier_than_before(self):
        """classify_edits still catches these, but now they never even stage."""
        for rel in (".env", "secrets/router_token", "ava.yaml"):
            with self.assertRaises(PermissionError, msg=rel):
                coder._stage_write(rel, "pwned", {})

    def test_traversal_out_of_the_repo_is_refused(self):
        for rel in ("../outside.py", "../../etc/passwd"):
            with self.assertRaises((ValueError, PermissionError), msg=rel):
                coder._tool_read_file(rel, {})

    def test_an_absolute_path_is_treated_as_repo_relative(self):
        """`/etc/passwd` is not an escape: _safe strips the leading slash, so it
        resolves INSIDE the repo (where no such file exists). Asserted because
        "it did not raise" and "it read /etc/passwd" look the same from a test
        that only checks for an exception."""
        out = coder._tool_read_file("/etc/passwd", {})
        self.assertNotIn("root:x:", out)
        self.assertIn("no such file", out)


class SearchGateTests(unittest.TestCase):
    def test_search_never_returns_a_line_from_a_denied_file(self):
        offenders = []
        # Deliberately broad patterns: "=" and "a" match nearly every text file,
        # so anything reachable will show up.
        for pattern in ("ANTHROPIC", "sk-", "PASSWORD", "TOKEN", "=", "a"):
            for line in coder._tool_search(pattern, None).splitlines():
                path = line.split(":", 1)[0]
                if path and access_policy.classify(path) == "denied":
                    offenders.append(f"{pattern!r} -> {line[:80]}")
        self.assertFalse(offenders, f"search leaked denied files: {offenders[:5]}")

    def test_search_still_finds_ordinary_code(self):
        out = coder._tool_search("def classify", "*.py")
        self.assertIn("access_policy.py", out)


class EveryToolRoutesThroughSafe(unittest.TestCase):
    """The gate is inheritable only while every tool actually uses it."""

    # Helpers that legitimately take no path.
    _NO_PATH = {"_tool_search"}
    # Filters inline instead of calling _safe per entry (it walks a directory).
    _SELF_FILTERS = {"_tool_search", "_tool_list_dir"}

    def test_every_tool_and_stage_helper_calls_safe(self):
        src = (ROOT / "ava_bridge" / "coder.py").read_text(encoding="utf-8")
        # Split into top-level function bodies.
        bodies: dict[str, str] = {}
        current = None
        for line in src.splitlines():
            m = re.match(r"^def (\w+)\(", line)
            if m:
                current = m.group(1)
                bodies[current] = ""
            elif current:
                bodies[current] += line + "\n"

        targets = [n for n in bodies
                   if n.startswith(("_tool_", "_stage_")) and n not in self._NO_PATH]
        self.assertTrue(targets, "no _tool_/_stage_ helpers found — did coder.py move?")

        offenders = [n for n in targets if "_safe(" not in bodies[n]]
        self.assertFalse(offenders, (
            "these code-agent helpers take a path but never resolve it through "
            "coder._safe(), so they inherit neither the traversal guard nor the "
            "access policy. Route the path through _safe() — or, if the helper "
            "walks a directory itself, filter with access_policy.classify() and "
            f"add it to _SELF_FILTERS here: {offenders}"))

    def test_the_self_filtering_helpers_consult_the_policy(self):
        src = (ROOT / "ava_bridge" / "coder.py").read_text(encoding="utf-8")
        for name in self._SELF_FILTERS:
            body = src.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]
            self.assertIn("access_policy.classify", body, (
                f"{name} does its own filesystem walk, so it bypasses _safe(); "
                "it must call access_policy.classify() per entry"))


class DenyListCoverageTests(unittest.TestCase):
    def test_the_secrets_this_repo_actually_keeps_are_denied(self):
        offenders = [rel for rel in _SECRETS
                     if access_policy.classify(rel) != "denied"]
        self.assertFalse(offenders, (
            "these hold credentials or self-gating config and must be 'denied': "
            f"{offenders}"))

    def test_git_tracked_source_is_not_accidentally_denied(self):
        """A deny glob that swallowed the repo would silently stop the agent
        working while looking like a security win."""
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "ava_bridge/*.py"],
                             capture_output=True, text=True, check=True).stdout
        tracked = [ln for ln in out.splitlines() if ln]
        self.assertTrue(tracked)
        offenders = [p for p in tracked if access_policy.classify(p) == "denied"]
        self.assertFalse(offenders, f"deny list is too broad: {offenders}")


if __name__ == "__main__":
    unittest.main()
