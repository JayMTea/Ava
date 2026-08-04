"""Nothing tracked may carry the owner's identity, a private sibling app, an
absolute home path, or a proprietary-tool watermark.

`CONTRIBUTING.md` states "Nothing personal in the repo" as a ground rule, and the
source tree honours it — but the rule was enforced by reading, and generated
artifacts are not read. Three tracked SVGs shipped for weeks carrying a 110px
unlicensed-copy watermark from a proprietary layout engine, the owner's name,
and the topology of three private sibling apps. Nobody re-reads a
46 KB SVG after a re-render, so nothing caught it. This test does.

Same shape as tests/test_diagram_sync.py: a static scan over `git ls-files` that
needs no bridge, no AVA_HOME, and no network — it only ever inspects files that
are actually tracked, so a fork's own local renders are unaffected.

Scope note: this checks CONTENT of tracked files. It cannot see git history, so a
secret already committed still needs `git filter-repo`. It is a ratchet against
new leaks, not a retroactive audit.
"""
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Extensions worth scanning. Binary formats are skipped: they cannot be reviewed
# in a diff either, and a leak there is a different (worse) problem.
_TEXTUAL = {".svg", ".md", ".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".yaml",
            ".yml", ".sh", ".toml", ".css", ".html", ".txt", ".d2", ".properties",
            # `.env.example` — suffix ".example" — is the file a bare-metal
            # forker is told to copy and edit, and it went unscanned for exactly
            # that reason: it named the maintainer's checkpoint in a sentence
            # about which model serves chat. Extension lists miss the files whose
            # extension is the interesting part.
            ".example", ".conf", ".cfg", ".ini", ".env", ".tmpl", ".template"}

# Tracked files worth scanning that have no useful suffix at all.
_TEXTUAL_NAMES = {"Dockerfile", "Makefile", ".gitignore", ".dockerignore"}

# Each entry: (compiled pattern, what to do about it).
_FORBIDDEN: list[tuple[re.Pattern, str]] = [
    # Assembled rather than written out, so this file does not itself become a
    # code-search hit for the watermark it is policing.
    (re.compile("UNLICENSED" + " COPY"),
     "a proprietary layout engine's watermark — re-render with a free engine "
     "(d2 --layout elk; see agent/docs/arch.py _FALLBACK_D2)"),
    (re.compile(r"/home/[a-z][a-z0-9_-]*/", re.I),
     "an absolute Linux home path — resolve it through ava_bridge.settings instead"),
    (re.compile(r"/Users/[a-z][a-z0-9_-]*/", re.I),
     "an absolute macOS home path — resolve it through ava_bridge.settings instead"),
    (re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
     "a Tailscale CGNAT address — use a hostname or an env var"),
    # The maintainer's own checkpoint. It is a 30B model needing ~35 GB of
    # weights, so shipping it as a DEFAULT hands every forker an install that
    # cannot start — and it names one person's hardware in a model-agnostic
    # product. The shipped default lives in deploy/default-model.env; this
    # model stays available as a documented upgrade in docs/CHOOSE_A_MODEL.md.
    # Matches the PROSE forms too ("open-model 30B", "the open-model model"), not
    # just the model id. The first version of this pattern was id-shaped only,
    # and every human-written sentence walked straight past it — including
    # README.md's second "Why Ava?" bullet, which told a forker that "Ava's
    # normal chat runs on the local Nemotron open-model 30B stack", and a Setup
    # hub hint that still called it "the default". A guard that catches the
    # machine-readable form and misses the sentence a human reads is backwards.
    #
    # Deliberately NOT matching a bare "30B": the parameter count is a model
    # CLASS, and "~30B-class models" is the fork-neutral phrasing this guard is
    # trying to produce. What must not appear is the checkpoint's NAME.
    # "always-on model" is the same checkpoint by a shorter name, and it slipped
    # past the id-shaped and prose-shaped patterns alike because it drops
    # "Nano". It shipped as a Vitals tile hint, so every forker's dashboard
    # labelled Route Errors with a model they do not run — and being a string in
    # frontend/src, it was baked into the tracked dist bundle too.
    (re.compile(r"Nemotron-Open|nano[-\s]omni|vllm-open|always-on\s+omni",
                re.I),
     "the maintainer's personal 30B checkpoint — use the shipped default from "
     "deploy/default-model.env, and document this one as an upgrade instead"),
    # The maintainer's machine. Real hostnames reached tracked docs twice via
    # pasted log lines labelled "straight off disk"; a stub reads identically.
    (re.compile(r"\bspark-[0-9a-f]{4}\b", re.I),
     "the maintainer's hostname from a pasted log line — stub it (e.g. "
     '"ava-host") so the sample stays fork-neutral'),
    # The maintainer's BOX described in prose, which is where it actually
    # survived: the id-shaped patterns above walk straight past "on the Spark"
    # and "on this GB10". phone_bridge.py's module docstring — the first file a
    # contributor opens — claimed "Everything runs locally on the Spark; nothing
    # leaves the tailnet", and neither clause is true of a fork: no Spark, and the
    # shipped compose has no tailnet. Same widening the checkpoint pattern got
    # after the id-shaped version missed the prose form in README.md.
    #
    # Scoped to prose that asserts THIS machine ("the Spark", "this GB10"). Naming
    # the hardware class is fine and often necessary — deploy/platforms.conf and
    # docs/evidence/ are about specific devices by design, and both are excluded
    # below.
    (re.compile(r"\b(?:the|our|my)\s+Spark\b|\bthis\s+GB10\b|\bon\s+my\s+(?:box|machine)\b",
                re.I),
     "the maintainer's own machine described in prose — name the hardware CLASS "
     'instead ("on unified-memory NVIDIA hardware"), or mark a measurement as '
     'one box\'s figure ("measured on a GB10; expect different figures")'),
]

# ---- Private sibling apps: patterns that must NOT be written down here -------
#
# Private app names leak the same way every time — not as a shipped directory
# (.git/info/exclude already stops that) but as a prose mention, a code comment,
# a docs table row, or a token env var. A capabilities page once tabulated two of
# them as if they shipped; the MCP sidecar's usage line named their token vars.
#
# The obvious fix — list the names in this file — is self-defeating: it publishes
# the exact strings the exclusion exists to keep private, in a tracked file, to a
# public repo. So the names live in a LOCAL, never-committed file instead:
#
#     .git/info/private-names      one regex per line, blank lines and # ignored
#
# On the maintainer's machine that file exists and the ratchet has teeth. On a
# fork it does not, this check is inert, and nothing has been disclosed. That
# asymmetry is correct: these are names only the maintainer can accidentally
# reintroduce, because only the maintainer has the apps.
def _git_info_dir() -> pathlib.Path:
    """The real `.git/info`, which is NOT `ROOT/.git/info` inside a worktree.

    In a linked worktree `ROOT/.git` is a *file* containing `gitdir: <path>`, so
    `ROOT/.git/info/private-names` raises NotADirectoryError, `_private_patterns`
    caught OSError, returned no patterns, and this guard passed while checking
    nothing. Measured: a branch developed in a worktree carried a forbidden name
    into a tracked test file and this test stayed green in that worktree — the same
    class of thing this repo keeps finding, a check reporting safety it does not
    have.

    `info/` is per-repository, not per-worktree, so a linked worktree must resolve
    to the COMMON dir. `git rev-parse --git-common-dir` is the supported way to ask.
    """
    try:
        out = subprocess.run(["git", "rev-parse", "--git-common-dir"],
                             cwd=ROOT, capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            p = pathlib.Path(out.stdout.strip())
            return (p if p.is_absolute() else ROOT / p).resolve() / "info"
    except (OSError, subprocess.SubprocessError):
        pass
    return ROOT / ".git" / "info"


_PRIVATE_NAMES_FILE = _git_info_dir() / "private-names"


def test_the_private_names_guard_is_not_silently_disabled() -> None:
    """A guard that reads no patterns passes trivially, so prove it read some.

    Skips only where there genuinely are none — a fork or a fresh clone — which is
    distinguishable from "could not find the file" now that the path resolves
    through --git-common-dir.
    """
    if not _PRIVATE_NAMES_FILE.exists():
        pytest.skip(f"no {_PRIVATE_NAMES_FILE} — a fork or a fresh clone")
    assert _private_patterns(), (
        f"{_PRIVATE_NAMES_FILE} exists but yielded no patterns, so every check "
        "below is vacuous.")


def _private_patterns() -> list[tuple[re.Pattern, str]]:
    try:
        raw = _PRIVATE_NAMES_FILE.read_text(encoding="utf-8")
    except OSError:
        return []                       # a fork, or a fresh clone — nothing to check
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append((re.compile(line, re.I),
                        "a private sibling app or one of its identifiers (matched "
                        f"{_PRIVATE_NAMES_FILE.name!r}) - use examples/device-app or "
                        "examples/home-assistant instead"))
        except re.error as e:
            raise AssertionError(f"{_PRIVATE_NAMES_FILE}: bad pattern {line!r}: {e}")
    return out

# Files that legitimately contain an otherwise-forbidden string.
_ALLOW = {
    # This file necessarily contains every pattern it bans.
    "tests/test_no_owner_identity.py",
    # Release history is a record of what shipped. Never rewritten to satisfy a
    # guard — the deferral is the decision, not an oversight.
    "CHANGELOG.md",
    # deploy/local-serve.sh's container is named `vllm-open` and things OUTSIDE
    # this repo reference it by that name (a sibling app's coordinator, and
    # ava_security_check.py). Renaming it is a coordinated change, not a sed.
    "deploy/local-serve.sh",
    "ava_security_check.py",
    # Its whole subject is classifying CGNAT binds, so it must name the
    # 100.64.0.0/10 network to define the range it tests. Every ADDRESS in it is
    # derived from that network at runtime rather than written down — the guard's
    # own instruction ("use a hostname or an env var") does not fit test data for
    # an IP classifier, but the risk it guards against (a copy-pasted literal that
    # turns out to be the owner's) is real, and deriving removes it.
    "tests/test_port_exposure_classes.py",
    # Fixtures asserting how a 30B model id is LABELLED and sized. The point of
    # these is the string handling, so the string has to be present.
    "tests/test_agent_brain.py",
    "tests/test_model_fit.py",
    "tests/test_hardware_models.py",
    "tools/mac_sim_audit.py",
    # These three name the 30B only to explain WHY the thing next to them is
    # model-agnostic — the incident is the documentation. Scrubbing the string
    # would leave a comment saying "deliberately generic" with no reason given,
    # and the next person to hardcode a checkpoint would have nothing to read.
    # A guard that deletes the record of the bug it prevents is a bad trade.
    "connectors/local-llm/connector.yaml",
    "deploy/docker-compose.yml",
    # docs/PACKAGING_PLAN.md used to be exempted here as "design history, dated
    # and superseded in place". It was also the one tracked doc written in the
    # owner's second person — "your Tailscale", "your daily use", "your own
    # apps" — so the file most likely to carry personal detail was the file this
    # guard did not read. It is no longer tracked (see
    # tests/test_no_internal_plans.py); planning docs live outside the repo.
}


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def _tracked_text(rel: str) -> str | None:
    """The tracked content of `rel` — from the worktree, else from HEAD.

    The worktree read used to be the only one, with `except OSError: continue` and
    the comment "a tracked file absent from the worktree is not our concern". It is
    exactly our concern. A file can be tracked and absent, and then this guard skips
    the one case where it is most needed: something committed by accident and since
    deleted locally is still in the tree everyone else clones.

    Measured, on 2026-07-30: 24 generated connector tools naming four private
    sibling apps were committed after a `.gitignore` rule moved off their directory.
    Deleting them locally made this test pass while every one of them was still in
    HEAD. Falling back to `git show HEAD:<rel>` closes that.
    """
    try:
        return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        pass
    try:
        out = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT,
                             capture_output=True, timeout=20)
        return out.stdout.decode("utf-8", errors="ignore") if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _scan() -> list[str]:
    offenders: list[str] = []
    rules = _FORBIDDEN + _private_patterns()
    for rel in _tracked_files():
        p = pathlib.Path(rel)
        if rel in _ALLOW:
            continue
        # The PATH is scanned before the extension filter, and regardless of it. A
        # private app's name reaches the tree as a directory name at least as easily
        # as it reaches it as prose — `.../connectors/<private-app>/x.mjs` leaks it
        # even if the file's bytes are clean, and a binary under such a directory
        # would have been skipped entirely.
        for pattern, why in rules:
            if pattern.search(rel):
                offenders.append(f"{rel}: {why} (in the PATH)")
        if p.suffix.lower() not in _TEXTUAL and p.name not in _TEXTUAL_NAMES:
            continue
        text = _tracked_text(rel)
        if text is None:
            continue
        for pattern, why in rules:
            if pattern.search(text):
                offenders.append(f"{rel}: {why}")
    return sorted(set(offenders))


def test_no_tracked_file_carries_owner_identity() -> None:
    offenders = _scan()
    assert not offenders, (
        "tracked files must stay fork-neutral (CONTRIBUTING.md: 'Nothing personal "
        "in the repo'). Fix each, or add a justified entry to _ALLOW in this file:"
        "\n  - " + "\n  - ".join(offenders)
    )


def test_topology_diagrams_are_not_tracked() -> None:
    """system/network render the local install's real topology (device labels,
    connected private apps) and their .d2 sources are gitignored, so a fork can
    neither regenerate nor correct them. Only security.svg — trust boundaries,
    app-agnostic, .d2 tracked — ships. See .gitignore for the rationale."""
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "agent/docs/diagrams/*.svg"],
        capture_output=True, text=True, check=True).stdout
    # Compare FULL paths, not basenames: a git pathspec glob matches recursively,
    # so this also returns agent/docs/diagrams/icons/network.svg — a legitimate
    # icon that merely shares a basename with the topology diagram.
    tracked = {ln for ln in out.splitlines() if ln}
    leaky = tracked & {f"agent/docs/diagrams/{n}"
                       for n in ("system.svg", "network.svg", "policy.svg")}
    assert not leaky, (
        f"{sorted(leaky)} render deployment-specific topology and must not be "
        "tracked — `git rm --cached agent/docs/diagrams/<file>`. The public "
        "architecture picture is docs/assets/architecture.svg."
    )
