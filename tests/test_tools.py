from pathlib import Path

import pytest

from aria.config import BrowserConfig, DesktopConfig
from aria.tools import (
    BrowserToolService,
    DesktopToolService,
    CustomToolRouter,
    ToolContext,
    ToolRegistry,
    register_filesystem_tools,
    register_shell_tool,
)


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_shell_tool(registry)
    register_filesystem_tools(registry)
    return registry


def test_custom_router_parses_multiple_calls_and_preserves_text() -> None:
    router = CustomToolRouter()
    parsed = router.parse(
        'Before '
        '<tool_call>{"id":"one","name":"read_file","arguments":{"path":"a.txt"}}</tool_call>'
        ' between '
        '<tool_call>{"name":"list_directory","arguments":{"path":"."}}</tool_call>'
        ' after'
    )

    assert parsed.content == "Before  between  after"
    assert [call.name for call in parsed.tool_calls] == ["read_file", "list_directory"]
    assert parsed.tool_calls[0].id == "one"
    assert parsed.tool_calls[1].id == "custom_call_1"
    assert parsed.errors == []


def test_custom_router_reports_invalid_calls() -> None:
    parsed = CustomToolRouter().parse('<tool_call>{"name":"read_file"</tool_call>')

    assert parsed.tool_calls == []
    assert parsed.errors == ["Invalid tool call JSON: Expecting ',' delimiter"]


def test_filesystem_tools_stay_inside_workspace(tmp_path: Path) -> None:
    registry = make_registry()
    context = ToolContext(tmp_path)
    result = registry.execute("read_file", {"path": "../outside.txt"}, context)
    assert result.is_error
    assert "inside the workspace" in result.output


def test_edit_file_requires_unambiguous_match(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("todo\ntodo\n", encoding="utf-8")
    result = make_registry().execute(
        "edit_file",
        {"path": "notes.txt", "old_text": "todo", "new_text": "done"},
        ToolContext(tmp_path),
    )
    assert result.is_error
    assert "replace_all" in result.output


def test_write_and_read_file(tmp_path: Path) -> None:
    registry = make_registry()
    context = ToolContext(tmp_path)
    write = registry.execute("write_file", {"path": "a/b.txt", "content": "hello"}, context)
    read = registry.execute("read_file", {"path": "a/b.txt"}, context)
    assert not write.is_error
    assert read.output == "hello"


def test_search_workspace_finds_matches(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
    nested = tmp_path / "src"
    nested.mkdir()
    (nested / "two.py").write_text("x = 1  # alpha here\n", encoding="utf-8")
    (tmp_path / "skipped").mkdir()
    (tmp_path / "skipped" / "three.py").write_text("alpha in git dir\n", encoding="utf-8")

    registry = make_registry()
    result = registry.execute("search_workspace", {"pattern": "alpha", "glob": "*.py"}, ToolContext(tmp_path))
    assert not result.is_error
    assert "one.py:1" in result.output
    assert str(Path("src") / "two.py") in result.output


def test_batch_edit_applies_multiple_edits(tmp_path: Path) -> None:
    (tmp_path / "code.txt").write_text("foo\nbar\nbaz\n", encoding="utf-8")
    registry = make_registry()
    result = registry.execute(
        "batch_edit",
        {
            "path": "code.txt",
            "edits": [
                {"old_text": "foo", "new_text": "FOO"},
                {"old_text": "baz", "new_text": "BAZ"},
            ],
        },
        ToolContext(tmp_path),
    )
    assert not result.is_error
    assert "applied 2/2" in result.output
    assert (tmp_path / "code.txt").read_text(encoding="utf-8") == "FOO\nbar\nBAZ\n"


def test_batch_edit_reports_failures(tmp_path: Path) -> None:
    (tmp_path / "code.txt").write_text("foo\n", encoding="utf-8")
    registry = make_registry()
    result = registry.execute(
        "batch_edit",
        {
            "path": "code.txt",
            "edits": [
                {"old_text": "missing", "new_text": "x"},
                {"old_text": "foo", "new_text": "FOO"},
            ],
        },
        ToolContext(tmp_path),
    )
    assert not result.is_error  # one applied, failures reported
    assert "applied 1/2" in result.output
    assert "old_text not found" in result.output


def test_desktop_managed_mode_only_allows_validated_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr("aria.tools.desktop.shutil.which", lambda _name: "/usr/bin/fake")
    monkeypatch.setattr(
        "aria.tools.desktop.subprocess.run",
        lambda command, **_kwargs: commands.append(command) or type("Result", (), {"stdout": "ok", "stderr": "", "returncode": 0})(),
    )
    service = DesktopToolService(DesktopConfig(enabled=True, mode="managed"))

    assert service.managed_dispatch({"dispatcher": "workspace", "arguments": ["2"]}) == "ok"
    with pytest.raises(ValueError, match="managed dispatcher"):
        service.managed_dispatch({"dispatcher": "exec", "arguments": ["rm", "-rf", "/"]})
    with pytest.raises(PermissionError, match="unrestricted"):
        service.dispatch({"dispatcher": "anything", "arguments": []})
    assert commands == [["hyprctl", "dispatch", "workspace", "2"]]


def test_desktop_unrestricted_registers_raw_tools() -> None:
    from aria.tools import ToolRegistry, register_desktop_tools

    registry = ToolRegistry()
    register_desktop_tools( registry, DesktopToolService(DesktopConfig(enabled=True, mode="unrestricted")))

    assert "desktop_raw_dispatch" in registry.names()
    assert "desktop_shell" in registry.names()
    assert "desktop_set_keybind" in registry.names()


def test_browser_output_paths_are_resolved_against_workspace(tmp_path: Path) -> None:
    service = BrowserToolService(BrowserConfig(), tmp_path)
    path = service._output_path("captures/page.png", ToolContext(tmp_path), "browser.png")

    assert path == (tmp_path / "captures" / "page.png").resolve()
