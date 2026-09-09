"""User-editable skills that shape ARIA's behavior.

Drop Markdown files into ``skills/`` and ARIA picks them up on the next
turn (no restart needed). Each file is a self-contained instruction block:
front-matter is optional, everything after it is the skill body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .logging_setup import log_debug, log_error, log_info

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_NAME_RE = re.compile(r"^[\w][\w .-]{0,63}$")


@dataclass(frozen=True)
class Skill:
    """One loaded skill file."""

    name: str
    content: str
    path: Path
    priority: int = 0

    @property
    def body(self) -> str:
        return self.content


class SkillLoader:
    """Read Markdown skill files from a directory, newest at each turn."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def load(self) -> list[Skill]:
        """Return all valid skills, sorted by priority (desc) then name."""
        if not self.directory.is_dir():
            return []
        skills: list[Skill] = []
        for path in sorted(self.directory.glob("*.md")):
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                log_error(f"Skills: could not read {path.name}: {exc}")
                continue
            skill = self._parse(path, raw)
            if skill is not None:
                skills.append(skill)
        skills.sort(key=lambda item: (-item.priority, item.name.lower()))
        log_debug(f"Skills: loaded {len(skills)} skill(s) from {self.directory}")
        return skills

    def _parse(self, path: Path, raw: str) -> Skill | None:
        front_matter: dict[str, str] = {}
        body = raw
        match = _FRONT_MATTER_RE.match(raw)
        if match:
            for line in match.group(1).splitlines():
                key, separator, value = line.partition(":")
                if separator:
                    front_matter[key.strip().lower()] = value.strip()
            body = raw[match.end():]
        name = front_matter.get("name", path.stem).strip()
        if not _NAME_RE.match(name):
            log_error(f"Skills: invalid name in {path.name}: {name!r}")
            return None
        body = body.strip()
        if not body:
            log_error(f"Skills: {path.name} has no content; skipped")
            return None
        try:
            priority = int(front_matter.get("priority", "0"))
        except ValueError:
            log_error(f"Skills: invalid priority in {path.name}; using 0")
            priority = 0
        return Skill(name=name, content=body, path=path, priority=priority)


def render_skills_section(skills: list[Skill]) -> str:
    """Render the loaded skills as a system-prompt section."""
    if not skills:
        return ""
    blocks: list[str] = ["ACTIVE SKILLS", "The following skill instructions are active for this conversation."]
    for skill in skills:
        blocks.append(f"--- SKILL: {skill.name} ---\n{skill.content}\n--- END SKILL ---")
    return "\n\n".join(blocks)


class SkillManager:
    """Owns the skills directory; refreshes and renders on demand."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._loader = SkillLoader(directory)

    def refresh(self) -> list[Skill]:
        """Re-read the skills directory (called every turn)."""
        return self._loader.load()

    def section(self) -> str:
        return render_skills_section(self.refresh())

    def describe(self) -> str:
        """One-line summary for /skills."""
        skills = self.refresh()
        if not skills:
            return f"No skills loaded from {self.directory}"
        names = ", ".join(skill.name for skill in skills)
        return f"{len(skills)} skill(s) from {self.directory}: {names}"

    def ensure_directory(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log_error(f"Skills: could not create {self.directory}: {exc}")


def inject_skills(system_prompt: str, section: str) -> str:
    """Place the skills section before CUSTOM TOOL PROTOCOL, or append it."""
    if not section:
        return system_prompt
    marker = "CUSTOM TOOL PROTOCOL"
    position = system_prompt.find(marker)
    if position > 0:
        return system_prompt[:position] + section + "\n\n" + system_prompt[position:]
    return system_prompt + "\n\n" + section
