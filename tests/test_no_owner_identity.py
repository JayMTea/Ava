"""Nothing tracked may carry the owner's identity, a private sibling app, an
absolute home path, or a proprietary-tool watermark.

`CONTRIBUTING.md` states "Nothing personal in the repo" as a ground rule, and the
source tree honours it — but the rule was enforced by reading, and generated
artifacts are not read. Three tracked SVGs shipped for weeks carrying a 110px
"" watermark from an unlicensed proprietary layout engine, the
owner's name, and the topology of three private sibling apps. Nobody re-reads a
46 KB SVG after a re-render, so nothing caught it. This test does.

Same shape as tests/test_no_eval_data.py and tests/test_diagram_sync.py: a static
scan over `git ls-files` that needs no bridge, no AVA_HOME, and no network — it
only ever inspects files that are actually tracked, so a fork's own local renders
are unaffected.

Scope note: this checks CONTENT of tracked files. It cannot see git history, so a
secret already committed still needs `git filter-repo`. It is a ratchet against
new leaks, not a retroactive audit.
"""
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Extensions worth scanning. Binary formats are skipped: they cannot be reviewed
# in a diff either, and a leak there is a different (worse) problem.
_TEXTUAL = {".svg", ".md", ".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".yaml",
            ".yml", ".sh", ".toml", ".css", ".html", ".txt", ".d2", ".properties"}

# Each entry: (compiled pattern, what to do about it).
_FORBIDDEN: list[tuple[re.Pattern, str]] = [
    (re.compile(r""),
     "a proprietary layout engine's watermark — re-render with a free engine "
     "(d2 --layout elk; see agent/docs/arch.py _FALLBACK_D2)"),
    (re.compile(r"/home/[a-z][a-z0-9_-]*/", re.I),
     "an absolute Linux home path — resolve it through ava_bridge.settings instead"),
    (re.compile(r"/Users/[a-z][a-z0-9_-]*/", re.I),
     "an absolute macOS home path — resolve it through ava_bridge.settings instead"),
    (re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
     "a Tailscale CGNAT address — use a hostname or an env var"),
]

# Files that legitimately contain an otherwise-forbidden string.
_ALLOW = {
    # This file necessarily contains every pattern it bans.
    "tests/test_no_owner_identity.py",
}


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def _scan() -> list[str]:
    offenders: list[str] = []
    for rel in _tracked_files():
        if rel in _ALLOW or pathlib.Path(rel).suffix.lower() not in _TEXTUAL:
            continue
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue  # a tracked file absent from the worktree is not our concern
        for pattern, why in _FORBIDDEN:
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
