"""Every Agent section the router can resolve is a section the view can render.

`components/agent/agentRoute.ts` is the address vocabulary and
`components/agent/AgentView.tsx` is the only thing that renders it. A section
listed in one and missing from the other is the failure this mirrors from
`test_hub_uniformity.py::test_every_agent_subtab_is_rendered`: the address
resolves, the tab highlights, and the body is blank — which reads as a broken
feature rather than as a missing branch.

Style: `git ls-files` + regex, no build, no browser. Runs anywhere.
"""
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
ROUTE = "frontend/src/components/agent/agentRoute.ts"
VIEW = "frontend/src/components/agent/AgentView.tsx"
APP = "frontend/src/App.tsx"

_SECTION = re.compile(r"\{\s*id:\s*'([a-z]+)'\s*,\s*label:")
_PANEL_BLOCK = re.compile(r"export const SIDE_PANELS = \[(.*?)\]", re.S)
_QUOTED = re.compile(r"'([a-z]+)'")


def _tracked() -> set[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    return set(out.splitlines())


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _sections() -> list[str]:
    body = _read(ROUTE).split("AGENT_SECTIONS", 1)[-1].split("]", 1)[0]
    return _SECTION.findall(body)


def test_the_scan_finds_the_sections() -> None:
    """A guard with no subjects agrees with everything."""
    have = _tracked()
    assert ROUTE in have, f"{ROUTE} is not tracked — point this guard at its new home"
    assert _sections(), (
        "no sections parsed out of AGENT_SECTIONS. The literal's shape changed, "
        "so this guard is passing vacuously — fix the pattern.")


def test_every_section_has_a_render_branch() -> None:
    src = _read(VIEW)
    missing = [s for s in _sections() if f"route.section === '{s}'" not in src]
    assert not missing, (
        f"AgentView.tsx renders no branch for {missing}. The address resolves "
        "and the tab highlights, so the failure looks like a broken feature "
        "rather than a missing `route.section === '<id>' && <Section/>`.")


def test_every_section_is_reachable_from_the_section_bar() -> None:
    """Rendered-but-unreachable is the mirror failure: a section only a
    hand-typed URL can get to."""
    src = _read(VIEW)
    assert "AGENT_SECTIONS.map(" in src, (
        "the section bar no longer derives from AGENT_SECTIONS, so a new "
        "section can be renderable and have no way to click to it.")


def test_the_agent_view_is_a_builtin_view() -> None:
    """`App.tsx` owns hash segment 0. A view the router below it can resolve but
    the shell does not list is unreachable by any address."""
    app = _read(APP)
    m = re.search(r"const BUILTIN_VIEWS\s*=\s*\[(.*?)\]", app)
    assert m, "BUILTIN_VIEWS is no longer a one-line literal this can parse"
    assert "'agent'" in m.group(1), "'agent' is missing from BUILTIN_VIEWS"


def test_the_agent_view_stays_mounted() -> None:
    """Agent must NOT follow the Data/Setup/Chats pattern of unmounting on tab
    switch: unmounting kills the terminal's PTY and the browser panel's snapshot
    state, which is the same reason iframe apps are kept in the tree.

    Pinned because the surrounding branches all look like the other pattern, so
    "tidying" this one to match them is a natural and silent regression.
    """
    app = _read(APP)
    assert "agentVisited" in app, (
        "the Agent view is no longer gated on a visited flag, so it is either "
        "always mounted (loading a chunk nobody asked for) or unmounted on "
        "switch (killing the terminal).")
    assert 'hidden={view !== \'agent\'}' in app, (
        "the Agent view is not being HIDDEN while inactive — if it is being "
        "unmounted instead, the terminal and browser panels lose their state "
        "every time the owner looks at another tab.")


def test_the_side_panel_vocabulary_is_declared_once() -> None:
    """SIDE_PANELS is parsed by the address router today and will be rendered by
    SidePanel.tsx when it lands. This asserts the list exists and is
    non-empty; the render-branch half joins it in the same commit as the panel
    component, so that guard never passes vacuously.
    """
    m = _PANEL_BLOCK.search(_read(ROUTE))
    assert m, "SIDE_PANELS is no longer a literal this can parse"
    panels = _QUOTED.findall(m.group(1))
    assert len(panels) >= 4, f"only {panels} declared; the address grammar needs them"
    view = ROOT / "frontend/src/components/agent/SidePanel.tsx"
    if view.exists():
        src = view.read_text(encoding="utf-8")
        missing = [p for p in panels if f"'{p}'" not in src]
        assert not missing, (
            f"SidePanel.tsx renders no branch for {missing}, so those addresses "
            "resolve to an empty panel.")
