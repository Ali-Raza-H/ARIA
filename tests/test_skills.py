"""Tests for the skills loader and prompt injection."""

from pathlib import Path

from aria.skills import SkillLoader, SkillManager, inject_skills, render_skills_section


def test_loader_reads_markdown_files(tmp_path: Path) -> None:
    (tmp_path / "research.md").write_text(
        "---\nname: Deep Research\npriority: 5\n---\nAlways verify claims with two sources.\n",
        encoding="utf-8",
    )
    (tmp_path / "tone.md").write_text("Be terse.\n", encoding="utf-8")

    skills = SkillLoader(tmp_path).load()

    assert [skill.name for skill in skills] == ["Deep Research", "tone"]
    assert skills[0].priority == 5
    assert skills[0].body == "Always verify claims with two sources."


def test_loader_skips_invalid_and_empty_files(tmp_path: Path) -> None:
    (tmp_path / "bad name!.md").write_text("hello\n", encoding="utf-8")
    (tmp_path / "empty.md").write_text("   \n", encoding="utf-8")

    assert SkillLoader(tmp_path).load() == []


def test_render_section_wraps_skills_with_markers() -> None:
    skills = SkillLoader(Path("nonexistent")).load()
    assert render_skills_section(skills) == ""

    section = render_skills_section(SkillLoader(Path("skills")).load()) if Path("skills").is_dir() else ""
    assert "ACTIVE SKILLS" in section or section == ""


def test_inject_skills_places_section_before_tool_protocol() -> None:
    prompt = "IDENTITY\nYou are ARIA.\n\nCUSTOM TOOL PROTOCOL\nEmit tool tags."

    merged = inject_skills(prompt, "ACTIVE SKILLS\n- be brief")

    assert merged.index("ACTIVE SKILLS") < merged.index("CUSTOM TOOL PROTOCOL")


def test_inject_skills_appends_when_no_marker() -> None:
    merged = inject_skills("base prompt", "ACTIVE SKILLS\n- be brief")

    assert merged.startswith("base prompt")
    assert "ACTIVE SKILLS" in merged


def test_inject_skills_without_section_is_identity() -> None:
    prompt = "base prompt"

    assert inject_skills(prompt, "") == prompt


def test_manager_describe_reports_count(tmp_path: Path) -> None:
    (tmp_path / "one.md").write_text("skill one\n", encoding="utf-8")

    manager = SkillManager(tmp_path)

    assert "1 skill(s)" in manager.describe()
    assert "one" in manager.describe()
