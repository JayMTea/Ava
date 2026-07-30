"""Convention guard: "supported engine" means something specific and checkable.

`docs/CHOOSE_A_MODEL.md` presented six engines as peers. Three of them had a UI
preset string and prose and nothing else — no health path, no launcher, no flag
row, no pull path. A user who picked MLX got a filled-in base URL and no further
help, which is not what "supported" reads as.

`ava_bridge/engines.py` now declares support as named affordances, and this guard
holds the declaration honest in both directions:

  * every engine the UI offers must be in the registry (so the UI cannot advertise
    something the backend has never heard of), and
  * every registry entry must carry a health path, a memory-gating decision, and —
    the one that actually bit — a RECORDED reason for its token-usage decision
    rather than a guess in a comment.

The usage rule deserves its own note. `router_app._STREAM_USAGE_ENGINES` used to
be a bare set whose comment asserted "llama.cpp builds may reject unknown params".
That is plausible and was never measured, and the cost of being wrong is silent:
token counts simply never appear. So an entry may be excluded, but it must say
whether anyone checked.
"""
import re

from gitfiles import ROOT, require_git

from ava_bridge import engines, model_fit, models, router_app, setup_wizard

_AGENT_PANEL = "frontend/src/components/hub/panels/AgentPanel.tsx"
_PRESET_VALUE = re.compile(r"\{\s*value:\s*'([a-z0-9.-]+)'")


def test_every_registry_entry_declares_the_affordances() -> None:
    assert engines.ENGINES, "the engine registry is empty"
    for e in engines.ENGINES:
        assert e.key and e.label, f"{e!r} is missing a key or label"
        assert e.tier in (engines.FIRST_CLASS, engines.GENERIC), (
            f"{e.key}: tier {e.tier!r} is not one of the two declared tiers. The "
            "vocabulary is closed on purpose — a third value like 'partial' is how "
            "a support claim stops being checkable.")
        assert e.health_path.startswith("/"), (
            f"{e.key}: health_path {e.health_path!r} must be a path. Without one "
            "the setup wizard cannot tell a running engine from an absent one, so "
            "it recommends cloud to someone who just started a local server.")
        assert e.usage_reason.strip(), (
            f"{e.key}: no reason recorded for its token-usage decision. Wrong "
            "either way, the symptom is that token counts silently vanish — so the "
            "reason is the only thing that makes the decision reviewable.")
        if not e.usage and not e.usage_verified:
            assert "NOT VERIFIED" in e.usage_reason, (
                f"{e.key}: excluded from usage on an UNVERIFIED basis, so say so. "
                "Write NOT VERIFIED in usage_reason (this repo's own doctrine) "
                "rather than stating a guess as a finding.")


def test_first_class_engines_are_launchable_and_feedable() -> None:
    """The bar for first-class: Ava can start it, or say plainly that it cannot."""
    for e in engines.first_class():
        if not e.local:
            continue                     # a cloud provider needs no launcher
        assert e.launcher, (
            f"{e.key} is first-class but has no launcher. Either give it one, or "
            "move it to GENERIC — 'first-class' has to mean Ava does something "
            "for you beyond filling in a URL.")
        assert e.pull, (
            f"{e.key} is first-class but declares no way for weights to arrive. "
            "Say 'bring your own' explicitly if that is the answer.")


def test_the_router_and_wizard_derive_from_the_registry() -> None:
    """No restated copies: the three consumers must agree by construction."""
    assert router_app._STREAM_USAGE_ENGINES == engines.usage_engines(), (
        "router_app._STREAM_USAGE_ENGINES has diverged from the engine registry. "
        "Derive it (engines.usage_engines()) rather than restating it — the "
        "restated version is what carried an unmeasured guess as a comment.")
    assert setup_wizard._HEALTH_PATH == engines.health_paths(), (
        "setup_wizard._HEALTH_PATH has diverged from the engine registry. It "
        "listed three engines while the UI offered six presets, so half the "
        "presets could not be health-checked.")


def test_local_engines_are_memory_gated() -> None:
    """Anything that runs on this box must be gated by model-fit.

    A local engine missing from `model_fit._LOCAL_ENGINES` is treated as a cloud
    API and never memory-checked — the "wrong, there is room" failure that ends in
    an OOM-killed render. `gguf` was missing exactly this way.
    """
    missing = sorted(engines.local_engines() - model_fit._LOCAL_ENGINES)
    assert not missing, (
        f"these engines run locally but are not in model_fit._LOCAL_ENGINES: "
        f"{missing}. They would be treated as cloud APIs and never memory-gated.")


def test_the_ui_offers_nothing_the_registry_has_not_heard_of() -> None:
    require_git()
    src = (ROOT / _AGENT_PANEL).read_text(encoding="utf-8")
    block = src.split("ENGINE_PRESETS", 1)
    assert len(block) > 1, f"{_AGENT_PANEL} no longer defines ENGINE_PRESETS"
    presets = set(_PRESET_VALUE.findall(block[1][:2000]))
    assert presets, (
        f"parsed no engine presets out of {_AGENT_PANEL} — did the shape change? "
        "Update _PRESET_VALUE, or this guard passes vacuously.")
    unknown = sorted(p for p in presets if engines.get(p) is None)
    assert not unknown, (
        f"{_AGENT_PANEL} offers engine preset(s) {unknown} that ava_bridge/engines.py "
        "does not know. The UI would fill in a base URL for something the backend "
        "cannot health-check, tune, or gate. Add a registry entry (GENERIC is a "
        "perfectly honest tier) or drop the preset.")


def test_engine_servable_here_agrees_with_declared_platforms() -> None:
    """A registry entry naming platforms must match the servability gate.

    Both exist and could disagree: `platforms` is documentation, and
    `models.engine_servable_here` is what actually refuses a multi-GB pull. A
    mismatch means the docs promise a platform the code declines, which is the
    defect class this whole phase is about.
    """
    from unittest import mock
    for e in engines.ENGINES:
        if not e.platforms:
            continue
        for plat in e.platforms:
            with mock.patch.object(models, "platform_label", return_value=plat):
                ok = models.engine_servable_here(e.key)
            if e.key == "vllm" and plat == "linux-amd":
                # Declared, but opt-in behind inference.allow_vllm_rocm — so the
                # gate says False by default and that is deliberate.
                assert not ok, (
                    "vLLM on linux-amd should be refused unless the owner opts in")
                continue
            assert ok, (
                f"{e.key} declares platform {plat!r} but engine_servable_here "
                f"refuses it there. Fix whichever is wrong — a doc that promises "
                "what the code declines is worse than either alone.")


def test_the_rendered_engine_table_matches_the_registry() -> None:
    """docs/CHOOSE_A_MODEL.md's table must be what --sync would write.

    Same shape as test_platform_matrix_ssot's docs check: the derived artifact is
    diffed against its source by a test needing no toolchain, so "promoted an
    engine in code, left the doc claiming otherwise" fails here.
    """
    require_git()
    rel = "docs/CHOOSE_A_MODEL.md"
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert engines.BEGIN in text, (
        f"{rel} lost its engines:begin marker, so the support table is now "
        "hand-maintained and will drift — which is exactly how it came to present "
        "six engines as peers.")
    assert engines.splice(text) == text, (
        f"{rel}'s engine table no longer matches ava_bridge/engines.py — run "
        "`python3 -m ava_bridge.engines --sync` and commit. Edit the registry, "
        "never the table.")
