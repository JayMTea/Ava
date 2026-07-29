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
    (re.compile(r"Nemotron-Open|nano[-\s]omni|vllm-open", re.I),
     "the maintainer's personal 30B checkpoint — use the shipped default from "
     "deploy/default-model.env, and document this one as an upgrade instead"),
    # Private sibling apps. These live outside this repo and are excluded by name
    # in .git/info/exclude precisely so the names are not published — but the
    # exclusion only stops the DIRECTORIES shipping, not a prose mention, a code
    # comment, a docs table, or a token name. Every one of these leaked that way
    # at least once: a capabilities page tabulated two of them as if they shipped,
    # the SDK sidecar's usage line named their token env vars, and .gitignore
    # itself listed one. A forker cannot obtain any of them, so naming them
    # documents a product that does not exist and identifies the maintainer's
    # other work. Use the shipped examples/ apps in documentation instead.
    (re.compile(r"\bledger\b|\bava-notes\b|persona[-_]studio|LedgerBackend"
                r"|\bnutrifit\b|MYAPP_TOKEN|MYAPP_MCP_TOKEN", re.I),
     "a private sibling app (or its token env var) — use examples/hello-app, "
     "examples/device-app or examples/home-assistant in docs and comments"),
    # The maintainer's machine. Real hostnames reached tracked docs twice via
    # pasted log lines labelled "straight off disk"; a stub reads identically.
    (re.compile(r"\bspark-[0-9a-f]{4}\b", re.I),
     "the maintainer's hostname from a pasted log line — stub it (e.g. "
     '"ava-host") so the sample stays fork-neutral'),
]

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
    # Design history, dated and superseded in place. Same rule as CHANGELOG.md.
    "docs/PACKAGING_PLAN.md",
    # The ADR that RECORDS the watermark incident and the revert to ELK. It
    # quotes the banned string to explain why the decision was superseded —
    # same rule as the three entries above: a guard that deletes the record of
    # the bug it prevents is a bad trade. The renders themselves are clean
    # (agent/docs/arch.py STATIC_D2 now pins elk for both hero diagrams).
    "agent/docs/adr/0004-tala-layout-engine.md",
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
