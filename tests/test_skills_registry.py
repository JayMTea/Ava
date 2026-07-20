"""Skills registry mechanics (ava_bridge/skills.py): discovery, frontmatter
derivation, tool extraction, and the deploy-state signal — all against a
throwaway skills tree so it's hermetic and independent of the shipped skills."""
import importlib
import json

import pytest


def _write_skill(root, name, frontmatter, body=""):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    fm = "\n".join(f"{k}: {v}" for k, v in frontmatter.items())
    (d / "SKILL.md").write_text(f"---\n{fm}\n---\n{body}", encoding="utf-8")


@pytest.fixture()
def skills_env(tmp_path, monkeypatch):
    core = tmp_path / "agent" / "skills"
    overlay = tmp_path / "overlay" / "agent"
    data = tmp_path / "data"
    (overlay / "skills").mkdir(parents=True)
    data.mkdir(parents=True)
    monkeypatch.setenv("AVA_OVERLAY", str(overlay))
    monkeypatch.setenv("AVA_DATA_DIR", str(data))
    from ava_bridge import skills
    importlib.reload(skills)
    # Point discovery at the temp tree (CORE_DIR is resolved at import time).
    monkeypatch.setattr(skills, "CORE_DIR", str(core))
    return skills, core, overlay / "skills", data


def test_discovers_core_and_overlay(skills_env):
    skills, core, overlay, _ = skills_env
    _write_skill(core, "ava-weather",
                 {"name": "ava-weather",
                  "description": '"How Ava reports weather via her get_weather tool. '
                                 'Use whenever the user asks about weather."'})
    _write_skill(overlay, "my-private",
                 {"name": "my-private", "description": '"Private overlay skill."'})
    cat = skills.catalog(force=True)
    by = {s["id"]: s for s in cat}
    assert set(by) == {"ava-weather", "my-private"}
    assert by["my-private"]["source"] == "overlay"
    assert by["ava-weather"]["source"] == "core"


def test_title_and_summary_are_derived(skills_env):
    skills, core, _, _ = skills_env
    _write_skill(core, "ava-self-coding",
                 {"name": "ava-self-coding",
                  "description": '"How Ava fixes her own code. Use whenever the user '
                                 'asks to change something. Trigger keywords - fix, edit."'})
    s = skills.catalog(force=True)[0]
    assert s["title"] == "Self Coding"                     # humanized, brand-stripped
    assert s["summary"] == "How Ava fixes her own code."   # tail cut at "Use whenever"
    assert "Trigger keywords" not in s["summary"]


def test_explicit_frontmatter_overrides_derivation(skills_env):
    skills, core, _, _ = skills_env
    _write_skill(core, "ava-x",
                 {"name": "ava-x", "title": '"Custom Title"', "summary": '"Clean summary."',
                  "category": "learning", "icon": "chart",
                  "app": "myapp",
                  "tools": "[tool_one, tool_two]",
                  "description": '"raw model-facing text"'})
    s = skills.catalog(force=True)[0]
    assert s["title"] == "Custom Title"
    assert s["summary"] == "Clean summary."
    assert s["category"] == "learning"
    assert s["icon"] == "chart"
    assert s["app"] == "myapp"  # connector id → UI shows that app's accent
    assert s["tools"] == ["tool_one", "tool_two"]


def test_tool_extraction_ignores_argument_names(skills_env):
    skills, core, _, _ = skills_env
    # Description names the tool; the body call-table has single-word args that
    # must NOT be mistaken for tools (the real-world noise this guards against).
    _write_skill(core, "ava-gpu",
                 {"name": "ava-gpu",
                  "description": '"How Ava renders via her run_gpu_job tool."'},
                 body="Call it with `prompt`, `seed`, `steps`, `days` args.")
    s = skills.catalog(force=True)[0]
    assert s["tools"] == ["run_gpu_job"]


def test_deploy_state_transitions(skills_env):
    skills, core, _, data = skills_env
    _write_skill(core, "ava-weather",
                 {"name": "ava-weather", "description": '"Weather via get_weather."'})
    # No manifest yet → unknown (agent never provisioned).
    assert skills.catalog(force=True)[0]["deployed"] == "unknown"

    sha = skills.catalog(force=True)[0]["sha256"]
    manifest = data / "skills_deployed.json"
    manifest.write_text(json.dumps([{"name": "ava-weather", "sha256": sha}]))
    assert skills.catalog(force=True)[0]["deployed"] == "deployed"

    # Edit the skill after deploy → stale.
    _write_skill(core, "ava-weather",
                 {"name": "ava-weather", "description": '"Weather via get_weather, now edited."'})
    assert skills.catalog(force=True)[0]["deployed"] == "stale"

    # A skill absent from the manifest → undeployed.
    _write_skill(core, "ava-new", {"name": "ava-new", "description": '"Brand new."'})
    states = {s["id"]: s["deployed"] for s in skills.catalog(force=True)}
    assert states["ava-new"] == "undeployed"


def test_malformed_frontmatter_is_reported_not_crashing(skills_env):
    skills, core, _, _ = skills_env
    d = core / "broken"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("no frontmatter here, just text", encoding="utf-8")
    cat = skills.catalog(force=True)
    assert any(s["id"] == "broken" for s in cat)   # still shown, with fallbacks
    assert any(e["id"] == "broken" for e in skills.load_errors())


def test_ava_yaml_category_override_wins_over_frontmatter(skills_env, monkeypatch):
    skills, core, _, _ = skills_env
    _write_skill(core, "ava-weather",
                 {"name": "ava-weather", "category": "Daily",  # author hint
                  "description": '"Weather."'})
    _write_skill(core, "ava-gpu",
                 {"name": "ava-gpu", "description": '"Image."'})  # no frontmatter cat
    # Owner ava.yaml override: recategorize weather, categorize image.
    monkeypatch.setattr(skills.settings, "get", lambda dotted, default=None, **k: (
        {"ava-weather": "Home", "ava-gpu": "Creative"} if dotted == "skills.categories"
        else default))
    by = {s["id"]: s for s in skills.catalog(force=True)}
    assert by["ava-weather"]["category"] == "Home"      # override beat the "Daily" hint
    assert by["ava-gpu"]["category"] == "Creative"    # override supplied a category


@pytest.fixture()
def settings_sandbox(skills_env, tmp_path, monkeypatch):
    """Route settings writes to a throwaway ava.yaml so recategorization tests
    never touch the real config (same isolation as test_backends_manager)."""
    skills = skills_env[0]
    monkeypatch.setattr(skills.settings, "CONFIG_PATH", tmp_path / "ava.yaml")
    monkeypatch.setattr(skills.settings, "_CFG", {})
    return tmp_path / "ava.yaml"


def test_set_category_writes_owner_override(skills_env, settings_sandbox):
    skills, core, _, _ = skills_env
    _write_skill(core, "ava-weather",
                 {"name": "ava-weather", "description": '"Weather."'})
    skills.catalog(force=True)
    assert skills.set_category("ava-weather", "Home")
    assert skills.catalog()[0]["category"] == "Home"   # write invalidated the cache
    # Persisted to ava.yaml under the owner-owned map, keyed by directory.
    import yaml
    saved = yaml.safe_load(settings_sandbox.read_text())
    assert saved["skills"]["categories"]["ava-weather"] == "Home"
    # Clearing removes the override (back to the author hint — none here).
    assert skills.set_category("ava-weather", None)
    assert skills.catalog()[0]["category"] is None
    assert not skills.set_category("nonexistent", "X")


def test_rename_category_covers_hints_and_general(skills_env, settings_sandbox):
    skills, core, _, _ = skills_env
    _write_skill(core, "ava-weather",
                 {"name": "ava-weather", "category": "Daily", "description": '"W."'})
    _write_skill(core, "ava-gpu", {"name": "ava-gpu", "description": '"I."'})
    skills.catalog(force=True)
    # Renaming a group that only exists via author frontmatter hints works: the
    # affected skills get owner overrides with the new label.
    assert skills.rename_category("Daily", "Home") == 1
    by = {s["id"]: s["category"] for s in skills.catalog()}
    assert by["ava-weather"] == "Home"
    # The implicit General bucket is renamable too — its skills adopt the name.
    assert skills.rename_category("General", "Misc") == 1
    assert {s["id"]: s["category"] for s in skills.catalog()}["ava-gpu"] == "Misc"
    # No-ops: same name, empty names, unknown label.
    assert skills.rename_category("Home", "Home") == 0
    assert skills.rename_category("", "X") == 0
    assert skills.rename_category("NoSuchGroup", "X") == 0


def test_category_order_create_and_delete(skills_env, settings_sandbox):
    skills, core, _, _ = skills_env
    _write_skill(core, "ava-weather",
                 {"name": "ava-weather", "description": '"Weather."'})
    skills.catalog(force=True)
    # Owner-created categories exist (empty) via the order list.
    assert skills.create_category("Home")
    assert skills.create_category("Ops")
    assert not skills.create_category("   ")
    assert not skills.create_category("General")   # the implicit bucket isn't creatable
    assert skills.category_order() == ["Home", "Ops"]
    # Reorder trims, dedupes, and drops the always-last General bucket.
    assert skills.set_category_order(["Ops", "Home", "Ops", "General", " "]) == ["Ops", "Home"]
    assert skills.category_order() == ["Ops", "Home"]
    # Renaming keeps the group's position in the order list; an EMPTY
    # owner-created category is renamable too (0 skills touched).
    skills.set_category("ava-weather", "Ops")
    assert skills.rename_category("Ops", "Base") == 1
    assert skills.category_order() == ["Base", "Home"]
    assert skills.rename_category("Home", "Lab") == 0
    assert skills.category_order() == ["Base", "Lab"]
    # Delete drops the label from the order and clears overrides pointing at it.
    assert skills.delete_category("Base")
    assert skills.category_order() == ["Lab"]
    assert skills.catalog()[0]["category"] is None
    assert not skills.delete_category("NoSuchCategory")


def test_body_returns_markdown_sans_frontmatter(skills_env):
    skills, core, overlay, _ = skills_env
    _write_skill(core, "ava-weather",
                 {"name": "ava-weather", "category": "Daily",
                  "description": '"Weather via get_weather."'},
                 body="# Weather\n\nCall `get_weather` for conditions.\n")
    _write_skill(overlay, "my-private",
                 {"name": "my-private", "description": '"Private."'}, body="# Private\n")
    # Lookup by frontmatter name and by directory both resolve.
    b = skills.body("ava-weather")
    assert b["title"] == "Weather"
    assert b["body"].startswith("# Weather")
    assert "category:" not in b["body"] and "---" not in b["body"].split("\n")[0]
    assert skills.body("my-private")["body"].strip() == "# Private"
    assert skills.body("nonexistent") is None
