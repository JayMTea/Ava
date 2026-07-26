"""Connector transport honesty guard.

"MCP" must mean a real Model Context Protocol server — never "this connector
has tools". Setup once badged every row with tools as MCP (the condition was
`mcp || discover || actions > 0`), so of four connected apps all four claimed
MCP and only one was one. A label that is true of everything tells the owner
nothing, and here it actively misrepresented what Ava was speaking.

Two halves, matching the tests/test_diagram_sync.py convention-guard style
(static scans over `git ls-files`, no build, no browser, runs anywhere):

  * connectors.transport() is the single classifier, and it is exact.
  * no shipped manifest, and no frontend render path, may claim MCP without
    a real `mcp:` block behind it.
"""
import pathlib
import re
import subprocess

import yaml

from ava_bridge import connectors

ROOT = pathlib.Path(__file__).resolve().parents[1]
PANEL = ROOT / "frontend/src/components/hub/panels/ConnectorsPanel.tsx"


def _tracked(pattern: str) -> list[pathlib.Path]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", pattern],
                         capture_output=True, text=True, check=True).stdout
    return [ROOT / line for line in out.splitlines() if line]


# --- the classifier ---------------------------------------------------------
def test_transport_classifies_each_shape() -> None:
    """Every manifest shape resolves to exactly one honest transport."""
    cases = [
        ({"mcp": {"url": "http://x/mcp"}}, "mcp"),
        ({"mcp": {"command": ["srv"]}}, "mcp"),
        ({"actions": {"discover": {"list": "/tools", "call": "/call",
                                   "base": "http://x"}}}, "discover"),
        ({"actions": [{"id": "a", "path": "/a"}]}, "rest"),
        ({"actions": {"static": [{"id": "a", "path": "/a"}]}}, "rest"),
        ({"ui": {"embed": "iframe"}}, "none"),
        ({}, "none"),
    ]
    for manifest, want in cases:
        got = connectors.transport(manifest)
        assert got == want, f"{manifest} -> {got!r}, expected {want!r}"
    assert set(connectors.TRANSPORTS) == {"mcp", "discover", "rest", "none"}


def test_transport_never_calls_a_facade_mcp() -> None:
    """The exact regression: a discover facade / static REST app is NOT MCP.

    An ava-tools/1 facade is MCP-*shaped* — same tool schema, same call
    envelope — which is precisely why it kept getting mislabelled."""
    facade = {"actions": {"discover": {"list": "/tools", "call": "/call",
                                       "base": "http://127.0.0.1:9000"}}}
    rest = {"actions": [{"id": "x", "method": "GET", "path": "/x"}]}
    for m in (facade, rest):
        assert connectors.transport(m) != "mcp", (
            "transport() called a non-MCP connector 'mcp' — the facade is Ava's "
            "own protocol. Wrap it with sdk/host/ava_mcp to make it real MCP.")


def test_shipped_manifests_declare_what_they_speak() -> None:
    """Every shipped connector's transport is backed by the block it needs."""
    offenders = []
    for path in _tracked("connectors/*/connector.yaml"):
        m = yaml.safe_load(path.read_text()) or {}
        kind = connectors.transport(m)
        rel = path.relative_to(ROOT)
        if kind == "mcp" and not isinstance(m.get("mcp"), dict):
            offenders.append(f"{rel}: transport 'mcp' without an mcp: block")
        if kind == "mcp":
            spec = m["mcp"]
            if not (spec.get("url") or spec.get("command")):
                offenders.append(f"{rel}: mcp: block declares neither url nor command")
    assert not offenders, "\n  ".join(["manifest transport mismatch:"] + offenders)


# --- the render path --------------------------------------------------------
def test_ui_reads_transport_and_never_infers_mcp() -> None:
    """Setup must render the transport the backend classified, not re-derive it.

    Specifically: no `MCP` label may be produced from a condition mentioning
    `discover` or `actions`, which is how the old badge lied."""
    text = PANEL.read_text()

    assert "TRANSPORT_LABEL" in text and "c.transport" in text, (
        "ConnectorsPanel must render the backend's `transport` field "
        "(TRANSPORT_LABEL / c.transport), not infer the protocol in the UI.")

    # The old badge, in any spacing: <Badge …>APP</Badge> / <Badge …>MCP</Badge>
    for dead in (r"<Badge[^>]*>\s*APP\s*<", r"<Badge[^>]*>\s*MCP\s*<"):
        assert not re.search(dead, text), (
            "The APP / MCP category badges are gone on purpose: they are "
            "orthogonal axes, not sibling categories. Group by "
            "shared.connectorGroup() and show the transport in the meta line.")

    # A line that mentions MCP must not also branch on discover/actions.
    for line in text.splitlines():
        if "MCP" not in line or line.lstrip().startswith(("//", "*")):
            continue
        assert not re.search(r"\b(c\.discover|c\.actions)\b", line), (
            "'MCP' must never be derived from `discover` or `actions` — that is "
            f"the bug this guard exists for:\n    {line.strip()}")


def test_ui_groups_by_identity_not_transport() -> None:
    """Sections come from connectorGroup(); transport never picks the section."""
    shared = (ROOT / "frontend/src/components/hub/shared.ts").read_text()
    assert "export function connectorGroup" in shared, (
        "shared.connectorGroup() is the single grouping rule for Setup → Connectors.")
    for token in ("'devices'", "'apps'", "'tools'"):
        assert token in shared, f"connectorGroup must resolve {token}"
    # Grouping keys off identity (ui / device role), never the wire protocol.
    body = shared.split("export function connectorGroup", 1)[1].split("\n}", 1)[0]
    assert "transport" not in body, (
        "connectorGroup() must group by what a connector IS to the owner "
        "(device > app > tools), never by how its tools travel.")
