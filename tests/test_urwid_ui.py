"""Tests for the urwid TUI and the UI backend factory."""

import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import urwid

from aria.agent.aria import AriaAgent
from aria.config import AppConfig
from aria.ui.factory import AVAILABLE_BACKENDS, create_repl
from aria.ui.urwid_tui import (
    UrwidRepl,
    humanize_tool_args,
    humanize_tool_result,
    markdown_to_markup,
)


class DummyAgent:
    model_name = "test-model"
    max_iterations = 5

    class memory:  # noqa: N801 - attribute container
        messages: list = []

        @staticmethod
        def path() -> None:
            return None


def make_config(tmp_path: Path, backend: str = "urwid") -> AppConfig:
    return AppConfig(
        provider="ollama",
        model="test",
        providers={},
        workspace=tmp_path,
        max_iterations=5,
        command_timeout_seconds=None,
        max_command_output_chars=None,
        persona="jarvis",
        ui_backend=backend,
        # Persist runtime state inside tmp_path: /trace and /status must never
        # write the project's real data/state/aria-state.yaml during tests.
        runtime_state_path=tmp_path / "aria-state.yaml",
    )


def _rendered(markup_or_widget: Any) -> str:
    """Flatten urwid markup or a Text widget to plain text.

    Wraps with str() because urwid's stubs type ``get_text()[0]`` as
    ``str | bytes`` even though it is always ``str`` at runtime.
    """
    if isinstance(markup_or_widget, urwid.Widget):
        widget = cast("urwid.Text", markup_or_widget)
    else:
        widget = urwid.Text(cast("Any", markup_or_widget))
    return str(widget.get_text()[0])


def test_markdown_to_markup_strips_bold_and_links() -> None:
    markup = markdown_to_markup("See [docs](https://x.y) and **bold** and `code`.")

    flattened = str(markup)
    assert "docs" in flattened
    assert "https" not in flattened
    assert "**" not in flattened
    assert "`" not in flattened
    assert "bold" in flattened
    assert "code" in flattened


def test_markdown_to_markup_headings_and_bullets() -> None:
    markup = markdown_to_markup("# Title\n- item one\n> quote")

    flattened = str(markup)
    assert "Title" in flattened
    assert "•" in flattened
    assert "item one" in flattened
    assert "quote" in flattened
    assert "#" not in flattened


def test_markdown_to_markup_line_breaks_are_real_newlines() -> None:
    """urwid only breaks lines at literal newlines; blocks must not mash."""
    markup = markdown_to_markup("# Title\n\nPara one.\n\nPara two.")

    plain = _rendered(markup).splitlines()
    assert plain[0] == "Title"
    assert "Para one." in plain
    assert "Para two." in plain
    # Each paragraph sits on its own rendered line.
    assert len(plain) >= 3


def test_markdown_to_markup_code_fence() -> None:
    markup = markdown_to_markup("```python\nprint(1)\nprint(2)\n```")

    plain = _rendered(markup)
    assert "┌─ python" in plain
    assert "│ print(1)" in plain
    assert "│ print(2)" in plain
    assert "└─" in plain
    assert "```" not in plain


def test_markdown_to_markup_table_and_rule() -> None:
    markup = markdown_to_markup("| a | b |\n|---|---|\n| 1 | 2 |\n\n---")

    plain = _rendered(markup)
    assert "1  ·  2" in plain
    assert "---" not in plain  # separator row and hr are never shown raw
    assert "─" in plain  # rule becomes a drawn line


def test_humanize_tool_args_extracts_main_argument() -> None:
    assert humanize_tool_args(json.dumps({"command": "git status --short"})) == "git status --short"
    assert humanize_tool_args(json.dumps({"path": "/tmp", "depth": 3})) == "/tmp"
    assert humanize_tool_args(json.dumps({})) == ""
    assert humanize_tool_args("plain text") == "plain text"


def test_humanize_tool_result_summarizes_payload() -> None:
    payload = json.dumps({"output": "first line\nsecond line"})
    assert humanize_tool_result(payload) == "first line"

    assert humanize_tool_result(json.dumps({"error": "No such file"})) == "No such file"
    long = humanize_tool_result(json.dumps({"output": "x" * 300}))
    assert long.endswith("…") and len(long) <= 100


def test_cot_events_are_humanized(tmp_path: Path) -> None:
    repl = UrwidRepl(cast(AriaAgent, DummyAgent()), config=make_config(tmp_path))
    from aria.agent.base import AgentEvent

    call = repl._render_event_markup(
        AgentEvent(kind="tool_call", round=1, name="shell", detail=json.dumps({"command": "ls -la"}))
    )
    result = repl._render_event_markup(
        AgentEvent(kind="tool_result", round=1, name="shell", detail=json.dumps({"output": "total 0"}), ok=True)
    )

    assert call is not None
    assert result is not None
    assert _rendered(call) == "⚙ shell  ls -la"
    assert _rendered(result) == "✓ shell  total 0"


def test_logo_header_uses_one_text_widget_with_newlines(tmp_path: Path) -> None:
    repl = UrwidRepl(cast(AriaAgent, DummyAgent()), config=make_config(tmp_path))

    text = _rendered(repl._header())

    # The ASCII logo renders as six separate lines, not one wrapped blob.
    assert len(text.splitlines()) == 8  # 6 logo lines + tagline + stamp


def test_dispatch_runs_turn_on_worker_thread(tmp_path: Path) -> None:
    """Turns run off the UI thread so streaming repaints stay live."""
    repl = UrwidRepl(cast(AriaAgent, DummyAgent()), config=make_config(tmp_path))
    marker = threading.Event()

    class StreamingAgent(DummyAgent):
        @staticmethod
        def run(user_text: str, on_text=None, on_event=None):  # type: ignore[no-untyped-def]
            marker.wait(timeout=5)
            return "done"

    repl.agent = cast(AriaAgent, StreamingAgent())
    repl._dispatch("hello")

    # _dispatch returns while the turn is still running; only one worker.
    assert repl._turn_thread is not None and repl._turn_thread.is_alive()
    repl._dispatch("second")
    marker.set()
    repl._turn_thread.join(timeout=5)
    assert not repl._turn_thread.is_alive()


def test_post_ui_runs_inline_without_loop(tmp_path: Path) -> None:
    repl = UrwidRepl(cast(AriaAgent, DummyAgent()), config=make_config(tmp_path))
    seen: list[str] = []
    repl._post_ui(lambda: seen.append("x"))
    assert seen == ["x"]


def test_urwid_repl_records_transcript(tmp_path: Path) -> None:
    repl = UrwidRepl(cast(AriaAgent, DummyAgent()), config=make_config(tmp_path))

    repl._remember_input("Hello")
    repl._remember_response("Hi there")
    repl._remember_command("/status", "all good")

    assert any("You: Hello" in line for line in repl._plain_log)
    assert any("ARIA: Hi there" in line for line in repl._plain_log)
    assert any("/status" in line for line in repl._plain_log)


def test_urwid_repl_inherits_commands(tmp_path: Path) -> None:
    repl = UrwidRepl(cast(AriaAgent, DummyAgent()), config=make_config(tmp_path))

    repl._cmd_trace("keep")

    assert repl.keep_cot is True
    assert repl.show_cot is True


def test_urwid_dispatch_routes_commands_and_messages(tmp_path: Path) -> None:
    repl = UrwidRepl(cast(AriaAgent, DummyAgent()), config=make_config(tmp_path))

    repl._dispatch("/status")

    assert any("/status" in line for line in repl._plain_log)
    assert repl._input_history[-1] == "/status"


def test_factory_builds_urwid_by_default(tmp_path: Path) -> None:
    repl = create_repl(cast(AriaAgent, DummyAgent()), config=make_config(tmp_path))

    assert isinstance(repl, UrwidRepl)


def test_factory_builds_rich_legacy_backend(tmp_path: Path) -> None:
    from aria.ui.repl import Repl

    repl = create_repl(cast(AriaAgent, DummyAgent()), config=make_config(tmp_path, backend="rich"))

    assert type(repl) is Repl


def test_factory_rejects_unknown_backend(tmp_path: Path) -> None:
    try:
        create_repl(cast(AriaAgent, DummyAgent()), config=make_config(tmp_path, backend="curses"))
    except ValueError as exc:
        assert "ui.backend" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown backend")


def test_available_backends_list() -> None:
    assert AVAILABLE_BACKENDS == ("urwid", "rich")
