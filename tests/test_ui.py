from typing import cast

from rich.console import Console
from rich.text import Text

from aria.agent.aria import AriaAgent
from aria.ui.repl import _LOGO_LINES, Repl


class DummyAgent:
    model_name = "test-model"


def make_repl(width: int = 80, height: int = 24) -> Repl:
    console = Console(width=width, height=height, record=True, color_system=None)
    return Repl(cast(AriaAgent, DummyAgent()), console)


def test_layout_uses_the_entire_terminal_width() -> None:
    repl = make_repl(width=100)

    assert repl._layout_width() == 100
    assert repl.console.measure(repl._prompt_box()).maximum == 100
    assert repl.console.measure(repl._chat_header()).maximum == 100


def test_frame_contains_logo_and_pinned_prompt() -> None:
    repl = make_repl(width=80, height=40)
    repl._remember_input("Hello ARIA")
    repl._remember_response(repl._render_aria_body("Welcome back."))
    repl._redraw_screen()

    rendered = repl.console.export_text(clear=False)
    assert _LOGO_LINES[0].strip() in rendered
    assert "ADAPTIVE REASONING & INTELLIGENCE ASSISTANT" in rendered
    assert "Hello ARIA" in rendered
    assert "Welcome back." in rendered
    assert "YOU" in rendered
    assert "Type a message, or /help for commands" in rendered

    # The prompt panel is the last rendered block, below the conversation.
    assert rendered.rfind("YOU") > rendered.rfind("Welcome back.")


def test_transcript_paging_keeps_a_single_full_width_frame() -> None:
    repl = make_repl(width=80, height=24)
    for index in range(12):
        repl._remember_response(Text(f"message {index}"))

    repl._frame()
    assert repl._scroll_offset == 0
    assert repl._transcript_content_width() == 80

    repl._scroll_transcript("oldest")
    assert repl._scroll_offset == repl._max_scroll_offset
    # Scrolling is measured in rendered terminal rows, allowing long panels
    # to be read a page at a time rather than jumping whole messages.
    assert repl._scroll_offset > 0
    repl._scroll_transcript("newest")
    assert repl._scroll_offset == 0
    repl._scroll_transcript("older")
    assert repl._scroll_offset > 0

    repl._redraw_screen()
    for line in repl.console.export_text(clear=False).splitlines():
        assert len(line) <= 80


def test_streaming_frame_keeps_prompt_below_live_output() -> None:
    repl = make_repl(width=80, height=24)
    repl._streaming_body = repl._render_aria_body("A response arriving now.")
    repl._redraw_screen()

    rendered = repl.console.export_text(clear=False)
    assert rendered.rfind("A response arriving now.") < rendered.rfind("YOU")
