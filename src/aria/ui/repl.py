"""Rich terminal interface: a full-screen ARIA chat with a pinned prompt.

The screen is composed as one frame: ARIA's logo and status sit at the top,
the conversation fills the scrollable middle, and the full-width input box is
anchored at the bottom. The same frame is used while a response streams, so
the prompt never moves below the output.
"""

from __future__ import annotations

import json
import os
import sys
import termios
import tty
from collections.abc import Callable, Iterable
from dataclasses import replace as dataclass_replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.segment import Segment, Segments
from rich.text import Text

from ..agent.aria import AriaAgent, DeployCoderTool
from ..agent.base import AgentEvent
from ..agent.coder import CoderService
from ..config import AppConfig, save_runtime_state
from ..images import capture_clipboard, capture_screen, load_image
from ..proactive import ProfileStore
from ..llm.factory import ProviderManager
from ..logging.setup import log_error, log_info
from ..memory import MemoryStore, SessionMemory
from ..skills import SkillManager
from ..speech import SpeechController
from ..tools.web import WebToolService

HELP_TEXT = """\
[bold cyan]ARIA commands[/bold cyan]
  /help                    Show this help
  /model [name]            Show or switch the model (on the active provider)
  /models [provider]       List models for a provider
  /provider [name]         Show or switch the provider
  /providers               List configured providers
  /workspace [path]        Show or change the workspace directory
  /iterations [n]          Show or set ARIA's iteration limit
  /agent [n]               Show or set the coder agent's iteration limit
  /trace [on|off|keep]    Show/hide the live execution trace; keep retains it in the transcript
  /cot [on|off|keep]       Deprecated alias for /trace
  /tts [on|off|engine]     Toggle speech or switch engine (kokoro_hf|kokoro_local|chatterbox)
  /memory                  Show memory status (status|search|facts|summarize|promote|retention)
  /memory search <text>    Search semantic memories and chat history
  /memory wipe <scope> DELETE  Wipe all, session, or tier 1/2/3 memory
  /timer <action>         Create/control/list persistent timers and reminders
  /scheduler              Show scheduler and autonomous-action status
  /profile                Show locally inferred working-style traits
  /attach clipboard|path Attach an ephemeral PNG/JPEG/WebP image to next turn
  /screen                 Capture an ephemeral screen image for next turn
  /skills                  List active skill files from the skills/ folder
  /clear                   Start a fresh conversation and clear its persistent session
  /save [path]             Export the session transcript to a text file
  /status                  Overview of the current configuration
  /web                     Check local SearXNG web-search health
  /logs                    Show log file locations
  /ollama clear-vram       Unload all Ollama models from VRAM
  /quit, /exit             Leave (alias: Ctrl+C, Ctrl+D)

While the prompt is active, the mouse wheel scrolls the transcript. Home jumps
to the oldest messages and End returns to the newest messages.
Anything else is a message to ARIA."""

_LOGO_LINES = (
    "     █████╗  ██████╗ ██╗ █████╗ ",
    "    ██╔══██╗██╔══██╗██║██╔══██╗",
    "    ███████║██████╔╝██║███████║",
    "    ██╔══██║██╔══██╗██║██╔══██║",
    "    ██║  ██║██║  ██║██║██║  ██║",
    "    ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝  ╚═╝",
)

_MIN_TERMINAL_WIDTH = 8


def _aria_panel(content: RenderableType, streaming: bool = False, width: int | None = None) -> Panel:
    title = "ARIA" + ("  …" if streaming else "")
    return Panel(
        content,
        title=title,
        title_align="left",
        border_style="bright_cyan",
        expand=True,
        width=width,
        padding=(0, 1),
    )


def _coder_panel(text: str, width: int | None = None) -> Panel:
    return Panel(
        Text(text, style="bright_black"),
        title="ARIA · Coder",
        title_align="left",
        border_style="yellow",
        expand=True,
        width=width,
        padding=(0, 1),
    )


class Repl:
    """Interactive terminal shell for ARIA."""

    def __init__(
        self,
        agent: AriaAgent,
        console: Console | None = None,
        *,
        provider_manager: ProviderManager | None = None,
        config: AppConfig | None = None,
        speech: SpeechController | None = None,
        coder_service: CoderService | None = None,
        deploy_handler: DeployCoderTool | None = None,
        web_service: WebToolService | None = None,
        skill_manager: SkillManager | None = None,
        vision_config: Any | None = None,
        image_fallback_provider: Any | None = None,
        scheduler_service: Any | None = None,
        proactive_service: Any | None = None,
        show_cot: bool = True,
    ) -> None:
        self.agent = agent
        self.console = console or Console()
        self.provider_manager = provider_manager
        self.config = config
        self.speech = speech
        self.coder_service = coder_service
        self._deploy_handler = deploy_handler
        self.web_service = web_service
        self.skill_manager = skill_manager
        self.vision_config = vision_config
        self.image_fallback_provider = image_fallback_provider
        self.scheduler_service = scheduler_service
        self.proactive_service = proactive_service
        self.show_cot = config.show_cot if config is not None else show_cot
        self.keep_cot = config.keep_cot if config is not None else True
        self._transcript: list[RenderableType] = []
        self._streaming_body: RenderableType | None = None
        self._input_history: list[str] = []
        self._history_index: int | None = None
        self._scroll_offset = 0
        self._visible_transcript_count = 1  # rendered terminal rows, not messages
        self._max_scroll_offset = 0
        self._tui_screen_active = False

    def _persist_runtime_state(self) -> None:
        if not self.config:
            return
        try:
            save_runtime_state(
                self.config.runtime_state_path,
                self.config,
                show_cot=self.show_cot,
                keep_cot=self.keep_cot,
                coder_max_iterations=(
                    self.coder_service.max_iterations if self.coder_service else None
                ),
            )
        except OSError as exc:
            log_error(f"Repl: could not persist runtime state: {exc}")
            self._remember_command(
                "/settings",
                Text(f"Could not save settings: {exc}", style="yellow"),
            )

    def _layout_width(self) -> int:
        """Return the current terminal width, always querying the real terminal
        size directly so that raw-mode writes and Rich output agree."""
        try:
            width = os.get_terminal_size().columns
        except OSError:
            width = self.console.width
        return max(_MIN_TERMINAL_WIDTH, width)

    def _sync_console_width(self) -> None:
        """Force Rich's console to use the real terminal width.

        Rich caches its width measurement. After raw-mode escape sequences
        have been written directly to stdout the cached value can diverge from
        the actual terminal width, causing panels to be rendered at the wrong
        size and producing the split-screen effect visible when scrolling.
        Calling this before every frame build keeps the two in sync.
        """
        self.console._width = self._layout_width()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        self._enter_tui_screen()
        try:
            while True:
                self._redraw_screen()
                try:
                    user_text = self._prompt()
                except EOFError:
                    break
                user_text = user_text.strip()
                if not user_text:
                    continue
                if not self._input_history or self._input_history[-1] != user_text:
                    self._input_history.append(user_text)
                self._input_history = self._input_history[-100:]
                if user_text.lower() in {"/quit", "/exit"}:
                    break
                if user_text.startswith("/"):
                    self._handle_command(user_text)
                    continue

                self._remember_input(user_text)
                try:
                    # Skills are re-read every turn so edits to the Markdown
                    # files apply without restarting ARIA.
                    refresh_skills = getattr(self.agent, "refresh_skills", None)
                    if callable(refresh_skills):
                        refresh_skills()
                    self._run_turn(user_text)
                except KeyboardInterrupt:
                    self._remember_response(Text("Interrupted.", style="yellow"))
                except Exception as exc:
                    log_error(f"Repl: turn failed: {type(exc).__name__}: {exc}")
                    self._error_panel(f"{type(exc).__name__}: {exc}")
        except KeyboardInterrupt:
            pass
        finally:
            try:
                self.agent.memory.cleanup()
            finally:
                self._leave_tui_screen()

    def _enter_tui_screen(self) -> None:
        """Take ownership of the terminal while the full-screen TUI runs."""
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return
        sys.stdout.write(
            # Alternate screen prevents the terminal's scrollback viewport
            # from moving underneath the frame. SGR mouse reporting sends
            # wheel events to the TUI instead of letting the terminal scroll.
            "\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h"
        )
        sys.stdout.flush()
        self._tui_screen_active = True

    def _leave_tui_screen(self) -> None:
        """Restore the user's normal terminal screen and mouse behavior."""
        if not self._tui_screen_active:
            return
        sys.stdout.write("\x1b[?1006l\x1b[?1000l\x1b[?25h\x1b[0m\x1b[?1049l")
        sys.stdout.flush()
        self._tui_screen_active = False

    @staticmethod
    def _mouse_scroll_destination(button: int) -> str | None:
        """Translate an SGR mouse wheel button into a transcript direction."""
        if button < 64:
            return None
        return "older" if button % 2 == 0 else "newer"

    # -------------------------------------------------------- screen layout
    def _chat_header(self) -> RenderableType:
        stamp = datetime.now().strftime("%a %d %b %Y")
        logo = Group(
            *(Text(line, style="bold bright_cyan", justify="center") for line in _LOGO_LINES),
            Text("ADAPTIVE REASONING & INTELLIGENCE ASSISTANT", style="bold white", justify="center"),
            Text(f"{stamp}  ·  ready", style="bright_black", justify="center"),
        )
        return Panel(
            logo,
            border_style="bright_cyan",
            box=box.SIMPLE,
            expand=True,
            width=self._layout_width(),
            padding=(0, 1),
        )

    def _status_bar(self) -> Text:
        model = self.agent.model_name or "unknown model"
        speech = "TTS on" if self.speech and self.speech.enabled else "TTS off"
        cot = "Trace on" if self.show_cot else "Trace off"
        if self.keep_cot:
            cot += "+keep"
        return Text.assemble(
            ("  ● ", "green"),
            ("ONLINE", "bold green"),
            ("  │  ", "bright_black"),
            (model, "cyan"),
            ("  │  ", "bright_black"),
            (speech, "bright_black"),
            ("  │  ", "bright_black"),
            (cot, "bright_black"),
            ("  │  /help", "bright_black"),
        )

    def _renderable_height(self, renderable: RenderableType, width: int | None = None) -> int:
        options = self.console.options.update(width=width or self._layout_width())
        return len(list(self.console.render_lines(renderable, options=options)))

    def _transcript_content_width(self) -> int:
        return self._layout_width()

    def _render_transcript(self, available: int) -> tuple[list[RenderableType], int]:
        if not self._transcript:
            self._visible_transcript_count = 0
            return [], 0

        options = self.console.options.update(width=self._transcript_content_width())
        lines: list[list[Segment]] = []
        for entry in self._transcript:
            lines.extend(self.console.render_lines(entry, options=options))
        total = len(lines)
        self._max_scroll_offset = max(total - available, 0)
        self._scroll_offset = min(max(self._scroll_offset, 0), self._max_scroll_offset)
        end = total - self._scroll_offset
        start = max(0, end - available)
        selected = lines[start:end]
        flattened: list[Segment] = []
        for line in selected:
            flattened.extend(line)
            flattened.append(Segment.line())
        self._visible_transcript_count = max(len(selected), 1)
        return ([Segments(flattened)] if flattened else []), len(selected)

    def _scroll_transcript(self, destination: str) -> None:
        """Move the transcript viewport.

        The scroll offset is floored at zero here but intentionally NOT clamped
        against _max_scroll_offset because that value is stale until the next
        call to _render_transcript.  The authoritative upper-bound clamp runs
        inside _render_transcript so it always uses a freshly computed limit.
        """
        count = len(self._transcript)
        if not count:
            return
        if destination == "older":
            self._scroll_offset += max(self._visible_transcript_count, 1)
        elif destination == "newer":
            self._scroll_offset -= max(self._visible_transcript_count, 1)
        elif destination == "oldest":
            self._scroll_offset = self._max_scroll_offset
        else:  # "newest"
            self._scroll_offset = 0
        # Floor only — ceiling is enforced by _render_transcript.
        self._scroll_offset = max(self._scroll_offset, 0)

    def _frame(self) -> Group:
        """Build the complete screen frame.

        _sync_console_width() must be called before this so that Rich uses the
        same width as the raw TTY escape sequences written by the editor.
        """
        width = self._layout_width()
        height = self.console.size.height
        header = self._chat_header()
        status = self._status_bar()
        prompt = self._prompt_box()
        fixed_height = sum(self._renderable_height(r) for r in (header, status, prompt))
        available = max(height - fixed_height, 1)

        entries, used = self._render_transcript(available)
        content_width = self._transcript_content_width()
        if self._streaming_body is not None and self._scroll_offset == 0:
            live_entry = _aria_panel(self._streaming_body, streaming=True, width=content_width)
            live_height = self._renderable_height(live_entry, content_width)
            if used + live_height <= available or not entries:
                entries.append(live_entry)
                used += live_height

        output = Group(*entries, *[Text("") for _ in range(max(available - used, 0))])
        return Group(header, status, output, prompt)

    def _redraw_screen(self) -> None:
        """Clear and paint one full terminal frame with a bottom-pinned prompt."""
        self._sync_console_width()
        self.console.clear()
        self.console.print(self._frame(), end="")

    def _redraw_tty_frame(self, text: str, cursor: int) -> None:
        """Repaint the full frame then restore the raw input line.

        Flush stdout before and after the Rich print so that the ANSI clear
        sequence and the Rich output arrive at the terminal in the correct
        order and do not interleave, which is what caused the split-screen
        corruption when scrolling.
        """
        stream = sys.stdout
        self._sync_console_width()
        # Flush any pending raw bytes before Rich takes over stdout.
        stream.flush()
        stream.write("\x1b[2J\x1b[1;1H")
        stream.flush()
        self.console.print(self._frame(), end="")
        # Flush Rich's output before we write raw cursor-positioning escapes.
        stream.flush()
        # Put the raw editor line on the prompt's content row using an
        # absolute position. Relative cursor movement is fragile when Rich
        # wraps a frame at the terminal edge and was causing split redraws.
        prompt_row = max(self.console.size.height - 1, 1)
        stream.write(f"\x1b[{prompt_row};1H")
        self._draw_input_line(text, cursor)

    # ------------------------------------------------------------- prompt
    def _prompt_box(self) -> Panel:
        return Panel(
            Text("›  Type a message, or /help for commands", style="bright_white"),
            title="YOU",
            title_align="left",
            border_style="bright_green",
            expand=True,
            width=self._layout_width(),
            padding=(0, 1),
        )

    def _draw_input_line(self, text: str, cursor: int) -> None:
        stream = sys.stdout
        width = self._layout_width()
        content_width = max(width - 6, 1)
        start = max(0, min(cursor - content_width + 1, len(text) - content_width))
        visible = text[start : start + content_width]
        cursor_column = min(max(cursor - start, 0), content_width)
        line = f"│ › {visible:<{content_width}} │"
        stream.write("\r\x1b[2K\x1b[32m" + line + "\x1b[0m")
        stream.write(f"\r\x1b[{5 + cursor_column}G")
        stream.flush()

    def _read_tty_prompt(self) -> str:
        stream = sys.stdout
        input_stream = sys.stdin
        try:
            fd = input_stream.fileno()
            original = termios.tcgetattr(fd)
        except (AttributeError, OSError, termios.error):
            return input()

        chars: list[str] = []
        cursor = 0
        self._history_index = None

        stream.write("\x1b[2A\r")
        self._draw_input_line("", 0)

        def replace_line(value: str) -> tuple[list[str], int]:
            return list(value), len(value)

        try:
            tty.setraw(fd)
            while True:
                key = input_stream.read(1)

                if key in {"\r", "\n"}:
                    stream.write("\r\x1b[2B")
                    stream.flush()
                    return "".join(chars)

                if key == "\x03":  # Ctrl+C
                    termios.tcsetattr(fd, termios.TCSADRAIN, original)
                    raise KeyboardInterrupt
                if key == "\x04":  # Ctrl+D
                    if not chars:
                        termios.tcsetattr(fd, termios.TCSADRAIN, original)
                        raise EOFError
                    continue
                if key == "\x0c":  # Ctrl+L
                    self._redraw_tty_frame("".join(chars), cursor)
                    continue

                if key == "\x1b":
                    sequence = input_stream.read(2)
                    if sequence == "[<":
                        # SGR mouse format: ESC [ < button ; column ; row M.
                        mouse_event: list[str] = []
                        while True:
                            mouse_key = input_stream.read(1)
                            if not mouse_key:
                                break
                            mouse_event.append(mouse_key)
                            if mouse_key in {"M", "m"}:
                                break
                        event = "".join(mouse_event)
                        if event and event[-1] in {"M", "m"}:
                            try:
                                button = int(event[:-1].split(";", 1)[0])
                            except (TypeError, ValueError):
                                button = -1
                            destination = self._mouse_scroll_destination(button)
                            if destination is not None:
                                self._scroll_transcript(destination)
                                self._redraw_tty_frame("".join(chars), cursor)
                    elif sequence == "[H":
                        self._scroll_transcript("oldest")
                        self._redraw_tty_frame("".join(chars), cursor)
                    elif sequence == "[F":
                        self._scroll_transcript("newest")
                        self._redraw_tty_frame("".join(chars), cursor)
                    elif sequence == "[D" and cursor:
                        cursor -= 1
                        self._draw_input_line("".join(chars), cursor)
                    elif sequence == "[C" and cursor < len(chars):
                        cursor += 1
                        self._draw_input_line("".join(chars), cursor)
                    elif sequence in {"[5", "[6"}:
                        # PageUp/PageDown are deliberately not transcript
                        # shortcuts; consume the rest of the escape sequence.
                        input_stream.read(1)  # trailing '~'
                    elif sequence in {"[1", "[4", "[3"}:
                        input_stream.read(1)  # trailing '~'
                        if sequence == "[3" and cursor < len(chars):
                            del chars[cursor]
                            self._draw_input_line("".join(chars), cursor)
                    elif sequence == "[A":  # Up — history older
                        if self._input_history:
                            if self._history_index is None:
                                self._history_index = len(self._input_history)
                            self._history_index = max(0, self._history_index - 1)
                            chars, cursor = replace_line(self._input_history[self._history_index])
                        self._draw_input_line("".join(chars), cursor)
                    elif sequence == "[B":  # Down — history newer
                        if self._history_index is not None:
                            self._history_index += 1
                            if self._history_index >= len(self._input_history):
                                self._history_index = None
                                chars, cursor = [], 0
                            else:
                                chars, cursor = replace_line(self._input_history[self._history_index])
                        self._draw_input_line("".join(chars), cursor)
                    else:
                        # Unknown escape sequence — ignore without redrawing.
                        pass
                    continue

                if key in {"\x7f", "\b"}:
                    if cursor:
                        del chars[cursor - 1]
                        cursor -= 1
                elif key.isprintable():
                    chars.insert(cursor, key)
                    cursor += 1

                self._draw_input_line("".join(chars), cursor)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)
            stream.write("\x1b[0m")
            stream.flush()

    def _prompt(self) -> str:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return self._read_tty_prompt()
        return self.console.input("[bold green]You ›[/bold green] ")

    # --------------------------------------------------------- transcript
    def _remember_input(self, text: str) -> None:
        self._scroll_offset = 0
        self._transcript.append(
            Panel(
                Text(text),
                title="You",
                title_align="left",
                border_style="green",
                expand=True,
                padding=(0, 1),
            )
        )

    def _remember_response(self, body: RenderableType) -> None:
        self._scroll_offset = 0
        self._transcript.append(_aria_panel(body))

    def _remember_command(self, command: str, output: RenderableType | None = None) -> None:
        self._scroll_offset = 0
        self._transcript.append(
            Panel(
                output if output is not None else Text(""),
                title=f"⌘ {command}",
                title_align="left",
                border_style="bright_black",
                expand=True,
                padding=(0, 1),
            )
        )

    # ------------------------------------------------------------ chat turn
    def _run_turn(self, user_text: str) -> None:
        chunks: list[str] = []
        result = ""
        steps: list[Text] = []
        coder_notes: list[str] = []
        self._streaming_body = Text("")

        with Live(
            self._frame(),
            console=self.console,
            refresh_per_second=12,
            transient=True,
        ) as live:
            def update() -> None:
                body: RenderableType = self._render_aria_body("".join(chunks))
                if self.show_cot and steps:
                    body = Group(*steps, Text(""), body)
                if coder_notes:
                    body = Group(body, _coder_panel(coder_notes[-1], self._layout_width()))
                self._streaming_body = body
                live.update(self._frame(), refresh=True)

            def on_text(text: str) -> None:
                chunks.append(text)
                update()

            def on_coder(text: str) -> None:
                if coder_notes:
                    coder_notes[-1] += text
                else:
                    coder_notes.append(text)
                update()

            def on_event(event: AgentEvent) -> None:
                if not self.show_cot:
                    return
                line = self._render_event(event)
                if line is not None:
                    steps.append(line)
                    update()

            if self._deploy_handler:
                self._deploy_handler.set_stream_hook(on_coder, on_event)

            try:
                result = self.agent.run_with_attachments(user_text, on_text=on_text, on_event=on_event)
                update()
            finally:
                if self._deploy_handler:
                    self._deploy_handler.set_stream_hook(None, None)
                self._streaming_body = None

        self._remember_response(self._render_aria_body(result))
        if self.keep_cot and steps:
            # Re-render the execution trace above the final answer so it
            # remains in the transcript after the transient live frame ends.
            cot_panel = Panel(
                Group(*steps),
                title="Execution trace",
                title_align="left",
                border_style="bright_black",
                expand=True,
                padding=(0, 1),
            )
            self._transcript.insert(-1, cot_panel)
        if self.speech:
            self.speech.say(result)
            if self.speech.last_error:
                self._remember_command(
                    "/tts",
                    Text(f"Speech unavailable: {self.speech.last_error}", style="yellow"),
                )

    def _render_event(self, event: AgentEvent) -> Text | None:
        if event.kind == "round":
            return Text(f"◦ round {event.round}", style="bright_black italic")
        if event.kind == "tool_call":
            args = event.detail if len(event.detail) <= 120 else event.detail[:120] + "…"
            return Text.assemble(
                ("⚙ ", "cyan"),
                (event.name, "bold cyan"),
                (f" {args}", "bright_black"),
            )
        if event.kind == "tool_result":
            first_line = event.detail.splitlines()[0] if event.detail else ""
            shown = first_line if len(first_line) <= 100 else first_line[:100] + "…"
            mark, style = ("✓", "green") if event.ok else ("✗", "red")
            return Text.assemble((f"{mark} ", style), (event.name, style), (f" {shown}", "bright_black"))
        if event.kind == "limit":
            return Text(f"⚠ {event.detail}", style="yellow")
        return None

    def _render_aria_body(self, text: str) -> RenderableType:
        if not text.strip():
            return Text("- Working...")
        return Markdown(text)

    def _error_panel(self, message: str) -> None:
        self._remember_response(
            Panel(
                Text(message, style="red"),
                title="Error",
                title_align="left",
                border_style="red",
                expand=True,
                padding=(0, 1),
            )
        )

    # ------------------------------------------------------------- commands
    def _handle_command(self, line: str) -> None:
        parts = line.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        handlers: dict[str, Callable[[str], None]] = {
            "/help": self._cmd_help,
            "/model": self._cmd_model,
            "/models": self._cmd_models,
            "/provider": self._cmd_provider,
            "/providers": self._cmd_providers,
            "/workspace": self._cmd_workspace,
            "/iterations": self._cmd_iterations,
            "/agent": self._cmd_agent,
            "/trace": self._cmd_trace,
            "/cot": self._cmd_trace,
            "/tts": self._cmd_tts,
            "/memory": self._cmd_memory,
            "/timer": self._cmd_timer,
            "/scheduler": self._cmd_scheduler,
            "/profile": self._cmd_profile,
            "/attach": self._cmd_attach,
            "/screen": self._cmd_screen,
            "/skills": self._cmd_skills,
            "/clear": self._cmd_clear,
            "/save": self._cmd_save,
            "/status": self._cmd_status,
            "/web": self._cmd_web,
            "/logs": self._cmd_logs,
            "/ollama": self._cmd_ollama,
        }
        handler = handlers.get(command)
        if handler is None:
            self._remember_command(line, Text(f"Unknown command: {command}. Try /help.", style="yellow"))
            return
        try:
            handler(argument)
        except Exception as exc:
            log_error(f"Repl: command {command} failed: {type(exc).__name__}: {exc}")
            self._error_panel(f"{type(exc).__name__}: {exc}")

    # --------------------------------------------------- command handlers
    def _cmd_help(self, _argument: str) -> None:
        self._remember_command("/help", Text.from_markup(HELP_TEXT))

    def _cmd_providers(self, _argument: str) -> None:
        if not self.provider_manager:
            self._remember_command("/providers", Text("Provider switching unavailable.", style="yellow"))
            return
        self._remember_command("/providers", Text(self.provider_manager.describe()))

    def _cmd_provider(self, argument: str) -> None:
        if not self.provider_manager or not self.config:
            self._remember_command("/provider", Text("Provider switching unavailable.", style="yellow"))
            return
        if not argument:
            self._remember_command(
                "/provider",
                Text(f"provider: {self.config.provider}  model: {self.agent.model_name}"),
            )
            return
        if argument not in self.provider_manager.names():
            self._remember_command(
                "/provider",
                Text(
                    f"Unknown provider '{argument}'. Known: {', '.join(self.provider_manager.names())}",
                    style="yellow",
                ),
            )
            return
        model = self.provider_manager.active_model(argument) or (
            self.provider_manager.models_for(argument) or [self.config.model]
        )[0]
        try:
            provider = self.provider_manager.create(argument, model)
        except Exception as exc:
            self._error_panel(f"Could not initialize {argument}: {exc}")
            return
        previous_provider = self.config.provider
        self.provider_manager.set_active_model(argument, model)
        self.config = dataclass_replace(self.config, provider=argument, model=model)
        self.agent.provider = provider
        set_memory_provider = getattr(self.agent.memory, "set_provider", None)
        if callable(set_memory_provider):
            set_memory_provider(provider)
        if self.coder_service and self.config.coder.provider is None:
            self.coder_service.set_model(argument, model)
        self._persist_runtime_state()
        log_info(f"Repl: provider switched from {previous_provider} to {argument} model {model}")
        self._remember_command("/provider", Text(f"Provider: {argument}  model: {model}"))

    def _cmd_models(self, argument: str) -> None:
        if not self.provider_manager or not self.config:
            return
        name = argument or self.config.provider
        models: Iterable[str] = self.provider_manager.models_for(name)
        models = list(models)
        if not models:
            self._remember_command("/models", Text(f"No models found for {name}.", style="yellow"))
            return
        active = self.provider_manager.active_model(name)
        lines = Text()
        for model in models:
            marker = "* " if model == active else "  "
            lines.append(f"{marker}{model}\n")
        self._remember_command("/models", lines)

    def _cmd_model(self, argument: str) -> None:
        if not self.provider_manager or not self.config:
            return
        if not argument:
            self._remember_command("/model", Text(f"model: {self.agent.model_name}"))
            return
        provider_name = self.config.provider
        try:
            provider = self.provider_manager.create(provider_name, argument)
        except Exception as exc:
            self._error_panel(f"Could not switch model: {exc}")
            return
        self.provider_manager.set_active_model(provider_name, argument)
        self.config = dataclass_replace(self.config, model=argument)
        self.agent.provider = provider
        set_memory_provider = getattr(self.agent.memory, "set_provider", None)
        if callable(set_memory_provider):
            set_memory_provider(provider)
        if self.coder_service and self.config.coder.provider is None:
            self.coder_service.set_model(provider_name, argument)
        self._persist_runtime_state()
        log_info(f"Repl: model switched to {argument}")
        self._remember_command("/model", Text(f"Model: {argument}"))

    def _cmd_workspace(self, argument: str) -> None:
        if not self.config:
            return
        if not argument:
            self._remember_command("/workspace", Text(f"workspace: {self.config.workspace}"))
            return
        new_workspace = Path(argument).expanduser().resolve()
        if not new_workspace.is_dir():
            self._error_panel(f"Not a directory: {new_workspace}")
            return
        self.config = dataclass_replace(self.config, workspace=new_workspace)
        self.agent.context = dataclass_replace(self.agent.context, workspace=new_workspace)
        self._persist_runtime_state()
        log_info(f"Repl: workspace switched to {new_workspace}")
        self._remember_command("/workspace", Text(f"Workspace: {new_workspace}"))

    def _cmd_iterations(self, argument: str) -> None:
        if not self.config:
            return
        if not argument:
            self._remember_command("/iterations", Text(f"iterations: {self.agent.max_iterations}"))
            return
        if not argument.isdigit() or int(argument) <= 0:
            self._remember_command("/iterations", Text("Give me a positive integer.", style="yellow"))
            return
        self.agent.max_iterations = int(argument)
        self.config = dataclass_replace(self.config, max_iterations=int(argument))
        self._persist_runtime_state()
        log_info(f"Repl: max_iterations set to {argument}")
        self._remember_command("/iterations", Text(f"Iteration limit: {argument}"))

    def _cmd_agent(self, argument: str) -> None:
        if not self.coder_service or not self.config:
            self._remember_command("/agent", Text("Coder agent unavailable.", style="yellow"))
            return
        if not argument:
            status = "running" if self.coder_service.is_busy() else "idle"
            self._remember_command(
                "/agent",
                Text(
                    f"coder: {status}  max_iterations: {self.coder_service.max_iterations}  "
                    f"provider/model: {self.coder_service.provider_label()}"
                ),
            )
            return
        if not argument.isdigit() or int(argument) <= 0:
            self._remember_command("/agent", Text("Give me a positive integer.", style="yellow"))
            return
        self.coder_service.max_iterations = int(argument)
        self.config = dataclass_replace(
            self.config,
            coder=dataclass_replace(self.config.coder, max_iterations=int(argument)),
        )
        self._persist_runtime_state()
        log_info(f"Repl: coder max_iterations set to {argument}")
        self._remember_command("/agent", Text(f"Coder iteration limit: {argument}"))

    def _cmd_trace(self, argument: str) -> None:
        value = argument.lower()
        if value in {"", "status"}:
            detail = (
                f"execution trace: {'on' if self.show_cot else 'off'}  "
                f"keep: {'on' if self.keep_cot else 'off'}"
            )
            self._remember_command("/trace", Text(detail))
            return
        if value == "on":
            self.show_cot = True
        elif value == "off":
            self.show_cot = False
        elif value == "keep":
            self.keep_cot = not self.keep_cot
            self.config = dataclass_replace(self.config, keep_cot=self.keep_cot) if self.config else self.config
            self._persist_runtime_state()
            self._remember_command("/trace", Text(f"Keep execution trace in transcript: {'on' if self.keep_cot else 'off'}"))
            return
        else:
            self._remember_command("/trace", Text("Usage: /trace [on|off|keep]", style="yellow"))
            return
        self.config = dataclass_replace(self.config, show_cot=self.show_cot) if self.config else self.config
        self._persist_runtime_state()
        detail = (
            f"Execution trace: {'on' if self.show_cot else 'off'}  "
            f"keep: {'on' if self.keep_cot else 'off'}"
        )
        self._remember_command("/trace", detail)

    def _cmd_tts(self, argument: str) -> None:
        if not self.speech or not self.config:
            return
        value = argument.lower()
        current = self.config.speech
        if value in {"", "status"}:
            engine = self.speech.engine
            detail = f"speech: {'on' if self.speech.enabled else 'off'}  engine: {engine}"
            if self.speech.last_error:
                detail += f"\nlast error: {self.speech.last_error}"
            self._remember_command("/tts", Text(detail))
            return
        if value == "on":
            new_speech = dataclass_replace(current, enabled=True)
        elif value == "off":
            new_speech = dataclass_replace(current, enabled=False)
        elif value in {"kokoro", "kokoro_hf", "kokoro_local", "chatterbox"}:
            canonical_engine = "kokoro_hf" if value == "kokoro" else value
            if not self.speech.switch_engine(canonical_engine):
                self._remember_command(
                    "/tts",
                    Text("Engine must be kokoro_hf, kokoro_local, or chatterbox.", style="yellow"),
                )
                return
            new_speech = dataclass_replace(current, engine=canonical_engine, enabled=True)
        else:
            self._remember_command(
                "/tts",
                Text("Usage: /tts [on|off|kokoro_hf|kokoro_local|chatterbox]", style="yellow"),
            )
            return
        self.config = dataclass_replace(self.config, speech=new_speech)
        self.speech.update_config(new_speech)
        self._persist_runtime_state()
        detail = f"speech: {'on' if self.speech.enabled else 'off'}  engine: {new_speech.engine}"
        if self.speech.last_error:
            detail += f"\nlast error: {self.speech.last_error}"
        self._remember_command("/tts", Text(detail))

    def _cmd_ollama(self, argument: str) -> None:
        if argument.lower() not in {"clear", "clear-vram", "unload"}:
            self._remember_command("/ollama", Text("Usage: /ollama clear-vram", style="yellow"))
            return
        clear_vram = getattr(self.agent.provider, "clear_vram", None)
        coder_uses_ollama = self.coder_service and self.coder_service.provider_name == "ollama"
        aria_uses_ollama = self.config and self.config.provider == "ollama"
        if not callable(clear_vram) and self.provider_manager and (aria_uses_ollama or coder_uses_ollama):
            clear_vram = self.provider_manager.clear_ollama_vram
        if not callable(clear_vram):
            self._remember_command("/ollama", Text("The active provider is not Ollama.", style="yellow"))
            return
        try:
            unloaded = clear_vram()
            self._remember_command("/ollama clear-vram", Text(f"Unloaded {unloaded} Ollama model(s) from VRAM."))
        except Exception as exc:
            log_error(f"Repl: Ollama VRAM cleanup failed: {type(exc).__name__}: {exc}")
            self._error_panel(f"Could not clear Ollama VRAM: {type(exc).__name__}: {exc}")

    def _cmd_memory(self, argument: str) -> None:
        memory = self.agent.memory
        persistent = cast("Any", memory)
        status = getattr(persistent, "status", None)
        if not callable(status):
            messages = memory.messages
            roles = [message.get("role", "?") for message in messages]
            stats = ", ".join(f"{role}: {roles.count(role)}" for role in sorted(set(roles)))
            self._remember_command(
                "/memory",
                Text(f"Session memory: {len(messages)} messages ({stats})\nFile: {memory.path}"),
            )
            return

        parts = argument.split(maxsplit=1)
        action = parts[0].lower() if parts else "status"
        value = parts[1].strip() if len(parts) > 1 else ""
        if action == "status":
            details = cast(dict[str, object], status())
            self._remember_command(
                "/memory",
                Text("Persistent memory\n" + "\n".join(f"  {k}: {v}" for k, v in details.items())),
            )
        elif action == "facts":
            self._remember_command("/memory facts", Text(str(persistent.facts_text())))
        elif action == "search":
            if not value:
                self._remember_command("/memory search", Text("Usage: /memory search <text>", style="yellow"))
                return
            semantic = cast(list[dict[str, Any]], persistent.search(value, tier="semantic"))
            history = cast(list[dict[str, Any]], persistent.search(value, tier="history"))
            lines = ["SEMANTIC MEMORIES"] + [f"- {item['document']}" for item in semantic]
            lines += ["", "CHAT HISTORY"] + [
                f"- {item['metadata'].get('role', '?')}: {item['document']}" for item in history
            ]
            self._remember_command("/memory search", Text("\n".join(lines) or "No matches."))
        elif action in {"summarize", "promote"}:
            count = int(persistent.summarize_now())
            self._remember_command(f"/memory {action}", Text(f"Processed {count} pending memory job(s)."))
        elif action == "retention":
            result = cast(dict[str, int], persistent.retention())
            self._remember_command(
                "/memory retention",
                Text("Retention complete: " + ", ".join(f"{k}={v}" for k, v in result.items())),
            )
        elif action == "wipe":
            scope_parts = value.split()
            if (
                len(scope_parts) != 2
                or scope_parts[1] != "DELETE"
                or scope_parts[0].lower() not in {"all", "session", "1", "2", "3", "tier1", "tier2", "tier3"}
            ):
                self._remember_command(
                    "/memory wipe",
                    Text("Usage: /memory wipe <all|session|1|2|3> DELETE", style="yellow"),
                )
                return
            scope = scope_parts[0].lower().removeprefix("tier")
            persistent.wipe(scope)
            self._remember_command("/memory wipe", Text(f"Deleted memory scope: {scope}."))
        else:
            self._remember_command(
                "/memory",
                Text("Usage: /memory [status|search|facts|summarize|promote|retention|wipe]", style="yellow"),
            )

    def _cmd_timer(self, argument: str) -> None:
        if self.scheduler_service is None:
            self._remember_command("/timer", Text("Scheduler is disabled. Enable scheduler.enabled first.", style="yellow"))
            return
        parts = argument.split()
        if not parts or parts[0].lower() in {"list", "status"}:
            self._remember_command("/timer", Text(json.dumps(self.scheduler_service.list_timers(), indent=2)))
            return
        action = parts[0].lower()
        if action == "create" and len(parts) >= 4:
            name = parts[1]
            kind = parts[2]
            try:
                duration = float(parts[3])
            except ValueError:
                self._remember_command("/timer", Text("Duration must be seconds.", style="yellow"))
                return
            persistent = len(parts) < 5 or parts[4].lower() != "session"
            timer_id = self.scheduler_service.create_timer(name, kind, duration, persistent)
            self._remember_command("/timer", Text(f"Created {timer_id}. Use /timer start {timer_id}."))
            return
        if action in {"start", "pause", "resume", "restart", "finish", "stop", "cancel"} and len(parts) == 2:
            result = self.scheduler_service.control_timer(parts[1], action)
            self._remember_command("/timer", Text(json.dumps(result, indent=2)))
            return
        self._remember_command("/timer", Text("Usage: /timer create <name> <kind> <seconds> [session] | /timer <start|pause|resume|restart|finish|stop|cancel> <id> | /timer list", style="yellow"))

    def _cmd_scheduler(self, _argument: str) -> None:
        if self.config is None:
            return
        service = self.scheduler_service
        details = {
            "enabled": service is not None,
            "autonomy_enabled": self.config.autonomy.enabled,
            "allowed_categories": self.config.autonomy.allowed_categories,
            "workflow_directory": str(self.config.scheduler.workflow_directory),
            "database": str(self.config.scheduler.database),
            "jobs": len(service.store.jobs()) if service is not None else 0,
            "timers": len(service.list_timers()) if service is not None else 0,
        }
        self._remember_command("/scheduler", Text(json.dumps(details, indent=2)))

    def _cmd_profile(self, _argument: str) -> None:
        if self.proactive_service is None:
            self._remember_command("/profile", Text("Proactive analysis is unavailable.", style="yellow"))
            return
        data = self.proactive_service.profile.load()
        self._remember_command("/profile", Text(json.dumps(data, indent=2)))

    def _cmd_attach(self, argument: str) -> None:
        if self.vision_config is None or not bool(getattr(self.vision_config, "enabled", True)):
            self._remember_command("/attach", Text("Vision input is disabled.", style="yellow"))
            return
        try:
            if argument.lower() == "clipboard":
                attachment = capture_clipboard(self.vision_config)
            else:
                path = Path(argument).expanduser().resolve()
                attachment = load_image(path, self.vision_config)
            self.agent.attach_image(attachment)
            self._remember_command("/attach", Text(f"Attached ephemeral image from {attachment.source}; it will be used on the next message."))
        except Exception as exc:
            self._error_panel(f"Could not attach image: {type(exc).__name__}: {exc}")

    def _cmd_screen(self, _argument: str) -> None:
        if self.vision_config is None or not bool(getattr(self.vision_config, "enabled", True)):
            self._remember_command("/screen", Text("Vision input is disabled.", style="yellow"))
            return
        try:
            attachment = capture_screen(self.vision_config, self.agent.context.workspace)
            self.agent.attach_image(attachment)
            self._remember_command("/screen", Text("Attached an ephemeral screen capture for the next message."))
        except Exception as exc:
            self._error_panel(f"Could not capture screen: {type(exc).__name__}: {exc}")

    def _cmd_skills(self, _argument: str) -> None:
        if self.skill_manager is None:
            self._remember_command("/skills", Text("Skills are not enabled.", style="yellow"))
            return
        refresh_skills = getattr(self.agent, "refresh_skills", None)
        if callable(refresh_skills):
            refresh_skills()
        self._remember_command("/skills", Text(self.skill_manager.describe()))

    def _cmd_clear(self, _argument: str) -> None:
        clear_session = getattr(self.agent.memory, "clear_current_session", None)
        if callable(clear_session):
            clear_session()
            self.agent.ensure_system_prompt()
        else:
            self.agent.memory.cleanup()
            self.agent.memory = SessionMemory(self.agent.memory.path.parent, label="aria")
            self.agent.ensure_system_prompt()
        log_info("Repl: conversation cleared")
        self._transcript.clear()
        self._scroll_offset = 0
        self._remember_command(
            "/clear",
            Text("Fresh start. Current session memory cleared; persistent facts retained."),
        )

    def _cmd_status(self, _argument: str) -> None:
        if not self.config:
            return
        busy = self.coder_service.is_busy() if self.coder_service else False
        speech_state = "off"
        if self.speech and self.speech.enabled:
            speech_state = f"on ({self.config.speech.engine})"
        body = Text(
            "ARIA status\n"
            f"  provider/model : {self.config.provider} / {self.agent.model_name}\n"
            f"  workspace      : {self.config.workspace}\n"
            f"  iterations     : {self.agent.max_iterations}"
            f" (coder: {self.coder_service.max_iterations if self.coder_service else 'n/a'})\n"
            f"  speech         : {speech_state}\n"
            f"  coder agent    : {'running' if busy else 'idle'}\n"
            f"  ARIA key env   : {self.provider_manager.credential_env(self.config.provider, 'aria') if self.provider_manager else 'n/a'}\n"
            f"  coder key env  : {self.provider_manager.credential_env(self.coder_service.provider_name, 'coder') if self.provider_manager and self.coder_service else 'n/a'}\n"
            f"  session memory : {len(self.agent.memory.messages)} messages"
        )
        self._remember_command("/status", body)

    def _cmd_web(self, _argument: str) -> None:
        if not self.config:
            return
        if not self.config.web.enabled or self.web_service is None:
            self._remember_command("/web", Text("Web search is disabled.", style="yellow"))
            return
        online = self.web_service.health_check()
        status = "ONLINE" if online else "OFFLINE"
        detail = (
            f"Web Search\\n  SearXNG URL: {self.config.web.searxng_url}\\n"
            f"  Status: {status}\\n  JSON API: {'OK' if online else 'unavailable'}"
        )
        self._remember_command("/web", Text(detail, style="green" if online else "yellow"))

    def _cmd_logs(self, _argument: str) -> None:
        if not self.config:
            return
        directory = self.config.logging.directory
        path = directory / "aria.log"
        size = f"{path.stat().st_size / 1024:.1f} KiB" if path.exists() else "missing"
        status = "available" if path.exists() else "not created yet"
        lines = Text(
            f"Logs\n  location : {path.resolve()}\n"
            f"  size     : {size}\n"
            f"  status   : {status}\n"
            f"  level    : DEBUG (file) / {self.config.logging.console_level} (console)"
        )
        self._remember_command("/logs", lines)

    def _cmd_save(self, argument: str) -> None:
        target = (
            Path(argument).expanduser()
            if argument
            else Path(f"aria-session-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt")
        )
        try:
            width = self._layout_width()
            with open(target, "w", encoding="utf-8") as handle:
                console = Console(width=width, file=handle)
                console.print(self._chat_header())
                for entry in self._transcript:
                    console.print(entry)
                console.print(
                    Text(f"  {len(self._transcript)} entries · exported {datetime.now():%Y-%m-%d %H:%M}")
                )
            self._remember_command("/save", Text(f"Session exported to {target.resolve()}"))
        except OSError as exc:
            self._error_panel(f"Could not save session: {exc}")
