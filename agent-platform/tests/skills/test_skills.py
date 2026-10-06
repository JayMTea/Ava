from __future__ import annotations

from pathlib import Path

from runtime.config import parse_frontmatter
from runtime.skill_registry import SkillRegistry

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "platform"


def test_project_skills_follow_the_agent_skills_format(project_config):
    for name, skill in project_config.skills.items():  # may be empty - that is fine
        assert skill.path.name == name
        assert 20 <= len(skill.description) <= 1024
        assert skill.body.strip(), f"{name} has an empty body"


def test_skill_load_returns_body_and_resources(config):
    text = SkillRegistry(config.skills, FIXTURE).load("sample")
    assert "# Skill: sample" in text
    assert "skills/sample/references/notes.md" in text


def test_coding_agent_skills_are_identical_for_claude_and_codex():
    """.claude/skills (Claude Code) and .agents/skills (Codex) must never drift apart."""
    claude = {p.relative_to(ROOT / ".claude/skills") for p in (ROOT / ".claude/skills").rglob("*") if p.is_file()}
    codex = {p.relative_to(ROOT / ".agents/skills") for p in (ROOT / ".agents/skills").rglob("*") if p.is_file()}
    assert claude == codex
    for rel in claude:
        a = (ROOT / ".claude/skills" / rel).read_bytes()
        b = (ROOT / ".agents/skills" / rel).read_bytes()
        assert a == b, f"{rel} differs between .claude/skills and .agents/skills"


def test_coding_agent_skills_have_portable_frontmatter():
    for path in (ROOT / ".claude/skills").glob("*/SKILL.md"):
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert meta["name"] == path.parent.name
        assert len(meta["description"]) <= 1024
        assert body
