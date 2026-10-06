"""Skills use progressive disclosure (the Agent Skills format, agentskills.io).

Only each skill's name and description go into the system prompt. When the model decides
a skill is relevant it calls the `skill.load` tool, which returns the full SKILL.md body
plus the paths of its bundled resources (scripts/, references/, assets/). Resources are
read on demand with filesystem.read - so a large skill library costs almost no context.
"""

from __future__ import annotations

from .types import SkillSpec

RESOURCE_DIRS = ("scripts", "references", "assets")


class SkillRegistry:
    def __init__(self, skills: dict[str, SkillSpec], workspace_root):
        self._skills = skills
        self.root = workspace_root

    def __contains__(self, name: str) -> bool:
        return name in self._skills

    def catalog(self, names: list[str]) -> list[tuple[str, str]]:
        return [(n, self._skills[n].description) for n in names if n in self._skills]

    def load(self, name: str) -> str:
        skill = self._skills[name]
        resources = sorted(
            p.relative_to(self.root).as_posix()
            for d in RESOURCE_DIRS
            if (skill.path / d).is_dir()
            for p in (skill.path / d).rglob("*")
            if p.is_file() and p.name != ".gitkeep"
        )
        listing = "\n".join(f"- {r}" for r in resources) or "- (none)"
        return f"# Skill: {skill.name}\n\n{skill.body}\n\n## Bundled resources (read with filesystem.read)\n{listing}"
