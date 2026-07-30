"""Setup (Hub) UI uniformity guard — keeps the shared-primitive refactor from
rotting back into per-panel copies.

The nine Setup tabs (frontend/src/components/hub/) share one small set of
building blocks: the data/action hooks (hooks.ts), the view primitives
(ui/Tile, ui/Legend, ui/Badge, ui/StatRow, ui/HubMessage), and one `--tone`
colour system in hub.css. It's easy for the next feature to hand-roll a local
Badge, a fresh `.something-ic` icon tile, or a per-component `.tone-ok { color }`
rule and quietly re-fork the design. This static scan (no build, no browser —
runs anywhere) fails when that happens, with instructions.

Mirrors the tests/test_diagram_sync.py convention-guard style.
"""
import pathlib
import re
from gitfiles import tracked_paths as _tracked

ROOT = pathlib.Path(__file__).resolve().parents[1]
HUB = "frontend/src/components/hub"
CSS = ROOT / "frontend/src/styles/hub.css"




def test_shared_primitives_are_not_rehandrolled() -> None:
    """Badge and StatRow live only in hub/ui/ — no panel defines its own copy."""
    offenders = []
    for f in _tracked(f"{HUB}/*.tsx") + _tracked(f"{HUB}/**/*.tsx"):
        if "/ui/" in f.as_posix():
            continue
        text = f.read_text()
        for prim in ("Badge", "StatRow"):
            if re.search(rf"^\s*(export\s+)?function {prim}\(", text, re.M):
                offenders.append(f"{f.relative_to(ROOT)} redefines {prim}")
    assert not offenders, (
        "Setup panels must import Badge / StatRow from hub/ui, not redefine them:\n  "
        + "\n  ".join(offenders)
    )


def test_no_resurrected_duplicated_classes() -> None:
    """The consolidated CSS names must not come back under their old per-panel
    identities — use the shared primitives instead."""
    css = CSS.read_text()
    banned = {
        r"\.conn-legend": "use <Legend> / .legend*",
        r"\.conn-actions|\.mem-actions": "use .row-actions",
        r"\.conn-sep": "use .meta-sep",
        r"\.(conn|mem|hist|sys-feat|sys-gov|ov-card|hw-hero)-ic\b": "use <Tile> / .tile",
        r"\.bud-tone-": "use the shared .tone-* / --tone system",
        r"\.conn-stat\.(ok|warn|off)\b": "use .conn-stat + a .tone-* class",
    }
    hits = [f"{pat}  ({why})" for pat, why in banned.items() if re.search(pat, css)]
    assert not hits, "hub.css resurrected a consolidated class:\n  " + "\n  ".join(hits)


def test_one_tone_system() -> None:
    """Tone→colour is defined once (the six `.tone-* { --tone: … }` setters);
    no component re-maps a tone to a literal colour of its own."""
    css = CSS.read_text()
    setters = re.findall(r"^\.tone-(?:ok|warn|err|accent|info|muted)\s*\{\s*--tone:", css, re.M)
    assert len(setters) == 6, (
        f"expected 6 `.tone-* {{ --tone: … }}` setters, found {len(setters)} — "
        "the tone system's single source moved or was duplicated"
    )
    # A per-component tone→colour rule (excluding the sanctioned .tile.tone-muted
    # neutral-wash special case) means the tone system was forked again.
    per_component = [
        m.group(0) for m in re.finditer(
            r"\.[a-z][\w-]*\.tone-(?:ok|warn|err|accent|info)\s*\{\s*(?:color|background)", css)
    ]
    assert not per_component, (
        "per-component tone→colour rules found — set a `.tone-*` class and read "
        "var(--tone) instead:\n  " + "\n  ".join(per_component)
    )


def test_every_useResource_surfaces_its_error() -> None:
    """A `useResource` result's `error` must reach the user somehow.

    17 of 21 call sites destructured only `{ data }`, so a backend failure
    rendered Setup as tabs of permanent "Detecting hardware…" ellipses: every
    panel looked like it was still loading, forever, with the real error sitting
    unread in a variable nobody had named. There was no global "cannot reach
    Ava" banner either, so the whole page simply looked slow.

    Two shapes are accepted, because they suit different panels:
      * destructure `error` and render it yourself (ConnectorsPanel's pattern), or
      * bind the WHOLE result and hand it to <ResourceState> / <ResourceError>,
        which cannot drop the error because there is no field to omit.

    A panel rendering several sections from ONE resource should use the second
    with a single <ResourceError> at the top — three wrappers would show the same
    failure three times.
    """
    # Deliberately silent, with the reason. Same shape as _ALLOW elsewhere.
    allow = {
        # HubView reads /api/hub/system for ONE thing: naming the right restart
        # command in its banner. A failure there must not become an error the
        # user cannot act on — the banner simply omits the command, and Overview
        # (rendered inside it) already surfaces the same fetch failing.
        "frontend/src/components/hub/HubView.tsx",
    }
    offenders = []
    seen = set()
    for f in (_tracked("frontend/src/components/**/*.tsx")
              + _tracked("frontend/src/components/*.tsx")):
        rel = f.relative_to(ROOT).as_posix()
        if rel in seen:
            continue                     # the two globs overlap
        seen.add(rel)
        if "/ui/ResourceState.tsx" in rel or rel in allow:
            continue                     # the component itself, or justified
        text = f.read_text()
        surfaces = ("ResourceState" in text) or ("ResourceError" in text)
        for m in re.finditer(r"^\s*const\s+(\{[^}]*\}|\w+)\s*=\s*useResource\(", text, re.M):
            binding = m.group(1)
            line = text[:m.start()].count("\n") + 1
            if binding.startswith("{"):
                if "error" not in binding:
                    offenders.append(
                        f"{rel}:{line} destructures {binding.strip()} — the error is dropped")
            elif not surfaces:
                offenders.append(
                    f"{rel}:{line} binds `{binding}` but the file renders no "
                    "<ResourceState>/<ResourceError>")
    assert not offenders, (
        "these useResource results can fail with nothing shown to the user — "
        "either destructure `error` and render it, or pass the whole result to "
        "<ResourceState r={…}> / <ResourceError r={…}> from hub/ui/ResourceState:\n  "
        + "\n  ".join(offenders)
    )


def test_agent_status_reports_the_CONFIGURED_runtime_not_the_local_one() -> None:
    """`hub/agent.py` hardcoded `runtime.nemoclaw()`.

    On `agent.runtime: remote` — what the agent and full profiles use — the bridge
    container reported its OWN missing CLI, so the panel rendered
    "CLI: not installed / Sandbox: none / Tools: unavailable" with a not-ready badge
    while the remote agent was serving turns correctly. It then told the owner to run
    `ava agent provision --install`, which is the wrong machine entirely.
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "ava_bridge" / "hub"
           / "agent.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "agent_status")
    calls = {c.func.attr for c in ast.walk(fn)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
    assert "configured" in calls, (
        "agent_status does not call runtime.configured(), so it reports a runtime "
        "the operator did not choose.")
    assert "nemoclaw" not in calls or "location" in src, (
        "agent_status still resolves the local runtime with no `location` marker, "
        "so the panel cannot tell which host cli/sandbox describe.")


def test_the_agent_panel_hides_local_rows_on_a_remote_runtime() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "components"
           / "hub" / "panels" / "AgentPanel.tsx").read_text(encoding="utf-8")
    assert "st.location !== 'remote'" in src, (
        "the CLI/Sandbox rows are unconditional again, so a working remote agent "
        "renders as 'not installed'.")
    assert "this container is not the agent host" in src, (
        "the remediation note still tells a remote owner to install a local CLI.")
