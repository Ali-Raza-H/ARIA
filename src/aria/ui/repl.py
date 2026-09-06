"""Rich terminal interface: a full-screen ARIA chat with a pinned prompt.

The screen is composed as one frame: ARIA's logo and status sit at the top,
the conversation fills the scrollable middle, and the full-width input box is
anchored at the bottom. The same frame is used while a response streams, so
the prompt never moves below the output.
"""

from __future__ import annotations

import sys
import termios
import tty
from collections.abc import Callable, Iterable
from dataclasses import replace as dataclass_replace
from datetime import datetime
from pathlib import Path

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from ..agent.aria import AriaAgent, DeployCoderTool
from ..agent.base import AgentEvent
from ..agent.coder import CoderService
from ..config import AppConfig, save_runtime_state
from ..llm.factory import ProviderManager
from ..logging_setup import log_error, log_info
from ..memory import SessionMemory
from ..speech import SpeechController

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
  /cot [on|off]            Show/hide the live chain of thought (tool calls + results)
  /tts [on|off|engine]     Toggle speech or switch engine (kokoro_hf|kokoro_local|chatterbox)
  /memory                  Show session memory statistics
  /clear                   Start a fresh conversation
  /save [path]             Export the session transcript to a text file
  /status                  Overview of the current configuration
  /logs                    Show log file locations
  /quit, /exit             Leave (alias: Ctrl+C, Ctrl+D)
Anything else is a message to ARIA."""

_LOGO_LINES = (
    "     █████╗  ██████╗ ██╗ █████╗ ",
    "    ██╔══██╗██╔══██╗██║██╔══██╗",
    "    ███████║██████╔╝██║███████║",
    "    ██╔══██║██╔══██╗██║██╔══██║",
    "    ██║  ██║██║  ██║██║██║  ██║",
    "    ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝  ╚═╝",
)

# The layout deliberately uses the complete terminal width. Keeping this in
# one place makes panels, dividers, and the prompt resize together.
_MIN_TERMINAL_WIDTH = 8

# Transcript entries are rendered newest-first into the available middle region.


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
        show_cot: bool = True,
    ) -> None:
        self.agent = agent
        self.console = console or Console()
        self.provider_manager = provider_manager
        self.config = config
        self.speech = speech
        self.coder_service = coder_service
        self._deploy_handler = deploy_handler
        self.show_cot = config.show_cot if config is not None else show_cot
        self._transcript: list[RenderableType] = []
        self._streaming_body: RenderableType | None = None
        self._input_history: list[str] = []
        self._history_index: int | None = None

    def _persist_runtime_state(self) -> None:
        """Save interactive preferences without modifying config.yaml."""
        if not self.config:
            return
        try:
            save_runtime_state(
                self.config.runtime_state_path,
                self.config,
                show_cot=self.show_cot,
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
        """Return the current terminal width used by every frame element."""
        return max(_MIN_TERMINAL_WIDTH, self.console.width)

    # ------------------------------------------------------------------ run
    def run(self) -> None:
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
                # Keep history useful without letting it grow forever.
                self._input_history = self._input_history[-100:]
                if user_text.lower() in {"/quit", "/exit"}:
                    break
                if user_text.startswith("/"):
                    self._handle_command(user_text)
                    continue

                self._remember_input(user_text)
                try:
                    self._run_turn(user_text)
                except KeyboardInterrupt:
                    self._remember_response(Text("Interrupted.", style="yellow"))
                except Exception as exc:
                    log_error(f"Repl: turn failed: {type(exc).__name__}: {exc}")
                    self._error_panel(f"{type(exc).__name__}: {exc}")
        except KeyboardInterrupt:
            pass
        finally:
            self.agent.memory.cleanup()

    # -------------------------------------------------------- screen layout
    def _chat_header(self) -> RenderableType:
        """Render a compact, resize-safe header."""
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
        """Render the single-line status/footer between header and chat."""
        model = self.agent.model_name or "unknown model"
        speech = "TTS on" if self.speech and self.speech.enabled else "TTS off"
        cot = "CoT on" if self.show_cot else "CoT off"
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

    def _renderable_height(self, renderable: RenderableType) -> int:
        """Measure the number of terminal rows a renderable will occupy."""
        options = self.console.options.update(width=self._layout_width())
        return len(list(self.console.render_lines(renderable, options=options)))

    def _render_transcript(self, available: int) -> tuple[list[RenderableType], int]:
        """Return the newest transcript entries that fit the output region."""
        shown: list[RenderableType] = []
        used = 0
        for entry in reversed(self._transcript):
            height = self._renderable_height(entry)
            if shown and used + height > available:
                break
            shown.append(entry)
            used += height
        shown.reverse()
        if len(shown) < len(self._transcript) and available > 0:
            omitted = len(self._transcript) - len(shown)
            hint = Text(
                f"  ↑ {omitted} earlier message{'' if omitted == 1 else 's'}",
                style="bright_black italic",
            )
            shown.insert(0, hint)
            used += 1
        return shown, used

    def _frame(self) -> Group:
        """Build the complete screen so output never pushes the prompt down."""
        width = self._layout_width()
        height = self.console.size.height
        header = self._chat_header()
        status = self._status_bar()
        prompt = self._prompt_box()
        fixed_height = sum(self._renderable_height(renderable) for renderable in (header, status, prompt))
        available = max(height - fixed_height, 1)

        entries, used = self._render_transcript(available)
        if self._streaming_body is not None:
            live_entry = _aria_panel(self._streaming_body, streaming=True, width=width)
            live_height = self._renderable_height(live_entry)
            if used + live_height <= available or not entries:
                entries.append(live_entry)
                used += live_height

        output = Group(
            *entries,
            *[Text("") for _ in range(max(available - used, 0))],
        )
        return Group(header, status, output, prompt)

    def _redraw_screen(self) -> None:
        """Clear and paint one full terminal frame with a bottom-pinned prompt."""
        self.console.clear()
        self.console.print(self._frame(), end="")

    # ------------------------------------------------------------- prompt
    def _prompt_box(self) -> Panel:
        """Render the fixed input area used by the raw TTY editor."""
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
        """Paint one editable row without allowing input to overwrite borders."""
        stream = sys.stdout
        width = self._layout_width()
        content_width = max(width - 6, 1)
        # Keep the right edge and both panel borders intact. The visible slice
        # follows the cursor when a message is wider than the available row.
        start = max(0, min(cursor - content_width + 1, len(text) - content_width))
        visible = text[start : start + content_width]
        cursor_column = min(max(cursor - start, 0), content_width)
        line = f"│ › {visible:<{content_width}} │"
        stream.write("\r\x1b[2K\x1b[32m" + line + "\x1b[0m")
        stream.write(f"\r\x1b[{5 + cursor_column}G")
        stream.flush()

    def _read_tty_prompt(self) -> str:
        """Read a single editable line while keeping the prompt pinned."""
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

        # Rich leaves the cursor immediately *after* the panel.
        # A Panel is three rows here: top border, content, bottom border.
        # Move up two rows so the raw editor draws over the content row,
        # not the bottom border.
        stream.write("\x1b[2A\r")
        self._draw_input_line("", 0)

        def replace_line(value: str) -> tuple[list[str], int]:
            return list(value), len(value)

        try:
            tty.setraw(fd)
            while True:
                key = input_stream.read(1)

                if key in {"\r", "\n"}:
                    # Return to column 1, then move from the content row past
                    # the bottom border so the next frame starts cleanly.
                    stream.write("\r\x1b[2B")
                    stream.flush()
                    return "".join(chars)

                if key == "\x03":  # Ctrl+C
                    raise KeyboardInterrupt
                if key == "\x04":  # Ctrl+D
                    if not chars:
                        raise EOFError
                    continue
                if key == "\x0c":  # Ctrl+L
                    # Clear and redraw the full frame while staying in raw mode.
                    stream.write("\x1b[2J\x1b[H")
                    self.console.print(self._frame(), end="")
                    stream.write("\x1b[2A\r")
                    self._draw_input_line("".join(chars), cursor)
                    continue

                if key == "\x1b":
                    sequence = input_stream.read(2)
                    if sequence == "[D" and cursor:
                        cursor -= 1
                    elif sequence == "[C" and cursor < len(chars):
                        cursor += 1
                    elif sequence == "[H":
                        cursor = 0
                    elif sequence == "[F":
                        cursor = len(chars)
                    elif sequence in {"[1", "[4", "[3"}:
                        # Home, End, and Delete use ESC [ 1/4/3 ~.
                        input_stream.read(1)
                        if sequence == "[1":
                            cursor = 0
                        elif sequence == "[4":
                            cursor = len(chars)
                        elif cursor < len(chars):
                            del chars[cursor]
                    elif sequence == "[A":  # history up
                        if self._input_history:
                            if self._history_index is None:
                                self._history_index = len(self._input_history)
                            self._history_index = max(0, self._history_index - 1)
                            chars, cursor = replace_line(self._input_history[self._history_index])
                    elif sequence == "[B":  # history down
                        if self._history_index is not None:
                            self._history_index += 1
                            if self._history_index >= len(self._input_history):
                                self._history_index = None
                                chars, cursor = [], 0
                            else:
                                chars, cursor = replace_line(self._input_history[self._history_index])
                    self._draw_input_line("".join(chars), cursor)
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
        """Read input inside the prompt row; non-TTY streams stay testable."""
        if sys.stdin.isatty() and sys.stdout.isatty():
            return self._read_tty_prompt()
        return self.console.input("[bold green]You ›[/bold green] ")

    # --------------------------------------------------------- transcript
    def _remember_input(self, text: str) -> None:
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
        self._transcript.append(_aria_panel(body))

    def _remember_command(self, command: str, output: RenderableType | None = None) -> None:
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
        """One conversational turn with a live-updating ARIA panel."""
        chunks: list[str] = []
        result = ""
        steps: list[Text] = []  # rendered chain-of-thought lines
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
                # The coder's streaming output is shown as a nested progress
                # note; the final report is rendered by ARIA afterwards.
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
                result = self.agent.run(user_text, on_text=on_text, on_event=on_event)
                update()
            finally:
                if self._deploy_handler:
                    self._deploy_handler.set_stream_hook(None, None)
                self._streaming_body = None

        # Commit the finished panel to the transcript so the next screen
        # repaint includes it (the transient Live display erased itself).
        self._remember_response(self._render_aria_body(result))
        if self.speech:
            self.speech.say(result)
            if self.speech.last_error:
                self._remember_command(
                    "/tts",
                    Text(f"Speech unavailable: {self.speech.last_error}", style="yellow"),
                )

    def _render_event(self, event: AgentEvent) -> Text | None:
        """One chain-of-thought line per agent event."""
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
        return None  # "text" events are rendered as the answer body itself

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
            "/cot": self._cmd_cot,
            "/tts": self._cmd_tts,
            "/memory": self._cmd_memory,
            "/clear": self._cmd_clear,
            "/save": self._cmd_save,
            "/status": self._cmd_status,
            "/logs": self._cmd_logs,
        }
        handler = handlers.get(command)
        # The loop's next _redraw_screen shows whatever the command recorded.
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

    def _cmd_cot(self, argument: str) -> None:
        value = argument.lower()
        if value in {"", "status"}:
            self._remember_command("/cot", Text(f"chain of thought: {'on' if self.show_cot else 'off'}"))
            return
        if value == "on":
            self.show_cot = True
        elif value == "off":
            self.show_cot = False
        else:
            self._remember_command("/cot", Text("Usage: /cot [on|off]", style="yellow"))
            return
        self.config = dataclass_replace(self.config, show_cot=self.show_cot) if self.config else self.config
        self._persist_runtime_state()
        self._remember_command("/cot", Text(f"Chain of thought: {'on' if self.show_cot else 'off'}"))

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
            # Selecting an engine is an explicit request to use speech, so
            # enable it even when the YAML default is disabled.
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

    def _cmd_memory(self, _argument: str) -> None:
        messages = self.agent.memory.messages
        roles = [message.get("role", "?") for message in messages]
        stats = ", ".join(f"{role}: {roles.count(role)}" for role in sorted(set(roles)))
        body = Text(
            f"Session memory: {len(messages)} messages ({stats})\nFile: {self.agent.memory.path}"
        )
        self._remember_command("/memory", body)

    def _cmd_clear(self, _argument: str) -> None:
        self.agent.memory.cleanup()
        self.agent.memory = SessionMemory(self.agent.memory.path.parent, label="aria")
        self.agent.ensure_system_prompt()
        log_info("Repl: conversation cleared")
        self._transcript.clear()
        self._remember_command("/clear", Text("Fresh start. Memory cleared and system prompt rebuilt."))

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
            f"  session memory : {len(self.agent.memory.messages)} messages"
        )
        self._remember_command("/status", body)

    def _cmd_logs(self, _argument: str) -> None:
        if not self.config:
            return
        directory = self.config.logging.directory
        lines = Text(f"Logs in {directory.resolve()}:\n")
        for name in ("debug.log", "info.log", "error.log"):
            path = directory / name
            size = f"{path.stat().st_size / 1024:.1f} KiB" if path.exists() else "missing"
            lines.append(f"  {name}: {size}\n")
        self._remember_command("/logs", lines)

    def _cmd_save(self, argument: str) -> None:
        """Export the session transcript to a text file."""
        target = (
            Path(argument).expanduser()
            if argument
            else Path(f"aria-session-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt")
        )
        width = self._layout_width()
        with open(target, "w", encoding="utf-8") as handle:
            console = Console(width=width, file=handle)
            console.print(self._chat_header())
            for entry in self._transcript:
                console.print(entry)
            console.print(Text(f"  {len(self._transcript)} entries · exported {datetime.now():%Y-%m-%d %H:%M}"))
        self._remember_command("/save", Text(f"Session exported to {target.resolve()}"))
