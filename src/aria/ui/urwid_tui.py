"""urwid terminal interface: the modern ARIA chat TUI.

``UrwidRepl`` reuses every command handler and configuration from the legacy
Rich :class:`~aria.ui.repl.Repl` and replaces only the rendering and input
layers: an urwid Frame pins the prompt to the bottom while the conversation
lives in a scrollable ListBox with native mouse-wheel support. The Rich REPL
remains available as the legacy backend (see ui.factory).

Two rendering notes that shape this module:

* urwid ``Text`` markup breaks lines only at a literal ``"\\n"`` element — a
  plain ``""`` between styled segments is a no-op, so every renderer here
  appends ``_sep()`` (a newline) to end a line. Getting this wrong mashes
  whole blocks (logo, replies, chain of thought) into one paragraph.
* Markdown is rendered by :func:`markdown_to_markup`, a small block-aware
  parser (fences, headings, lists, quotes, tables, rules, inline styles) so
  replies look structured instead of a wall of raw syntax.

Streaming: the agent turn runs on a worker thread while urwid's event loop
(an asyncio loop) keeps repainting. Callbacks from the worker are marshaled
back with :meth:`_post_ui` (``call_soon_threadsafe``), so the chain of
thought, streamed text, and coder notes update live — and scrolling/input
stay responsive during a turn — instead of the screen freezing until the
turn completes.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import urwid

from .repl import _LOGO_LINES, Repl
from ..logging_setup import log_error

# Bright-on-dark palette: terminal backgrounds are usually a very dark shade,
# so "dark gray" accents are nearly invisible. Dim text uses "light gray" and
# accents stay saturated (cyan/green/yellow/red).
PALETTE = [
    ("logo", "light cyan", ""),
    ("tagline", "white", ""),
    ("stamp", "light gray", ""),
    ("status", "white", ""),
    ("status-dim", "light gray", ""),
    ("user", "white", ""),
    ("user-title", "light green,bold", ""),
    ("aria-title", "light cyan,bold", ""),
    ("trace", "light gray", ""),
    ("trace-dim", "light gray", ""),
    ("trace-title", "white,bold", ""),
    ("trace-tool", "light cyan", ""),
    ("trace-ok", "light green", ""),
    ("trace-fail", "light red", ""),
    ("trace-warn", "yellow", ""),
    ("coder", "yellow", ""),
    ("coder-title", "yellow,bold", ""),
    ("command", "light gray", ""),
    ("command-title", "white", ""),
    ("error", "light red", ""),
    ("error-title", "light red,bold", ""),
    ("edit", "white", ""),
    ("edit-caption", "light green,bold", ""),
    ("footer", "light gray", ""),
    ("md-h1", "light cyan,bold", ""),
    ("md-h2", "light cyan,bold", ""),
    ("md-h3", "white,bold", ""),
    ("md-bold", "white,bold", ""),
    ("md-italic", "light gray", ""),
    ("md-code", "light blue", ""),
    ("md-link", "light blue,underline", ""),
    ("md-quote", "light gray", ""),
    ("md-bullet", "light cyan", ""),
    ("md-dim", "light gray", ""),
    ("md-table", "light gray", ""),
]

# --------------------------------------------------------------- markdown

_FENCE_RE = re.compile(r"^\s*```(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_ULIST_RE = re.compile(r"^(\s*)[-*+]\s+(.+)$")
_OLIST_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.+)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_CELL_SEP_RE = re.compile(r":?-+:?")
_INLINE_RE = re.compile(
    r"(?P<code>`[^`]+`)"
    r"|(?P<bold>\*\*[^*]+\*\*)"
    r"|(?P<italic>\*[^*\s][^*]*\*)"
    r"|(?P<link>\[(?P<label>[^\]]+)\]\([^)]*\))"
)

def _sep() -> str:
    """End a markup line: urwid only breaks lines at a literal newline."""
    return "\n"


def _emit(segments: list[Any], style: str, text: str) -> None:
    if text:
        segments.append((style, text) if style else text)


def _inline(text: str, style: str = "") -> list[Any]:
    """Render inline markdown (bold, italic, code, links) as urwid markup."""
    segments: list[Any] = []
    position = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > position:
            _emit(segments, style, text[position : match.start()])
        kind = match.lastgroup
        if kind == "code":
            _emit(segments, "md-code", match.group(0)[1:-1])
        elif kind == "bold":
            _emit(segments, "md-bold", match.group(0)[2:-2])
        elif kind == "italic":
            _emit(segments, "md-italic", match.group(0)[1:-1])
        else:  # a link: match ends inside the nested label group
            _emit(segments, "md-link", match.group("label"))
        position = match.end()
    _emit(segments, style, text[position:])
    return segments


def markdown_to_markup(text: str) -> list[Any]:
    """Convert a Markdown reply into urwid Text markup.

    Supports fenced code blocks (with a language tag), headings, ordered and
    nested unordered lists, block quotes, tables, horizontal rules, and the
    inline styles handled by :func:`_inline`.
    """
    markup: list[Any] = []
    in_fence = False
    fence_lang = ""

    for line in text.splitlines():
        fence = _FENCE_RE.match(line)
        if fence:
            if not in_fence:
                in_fence = True
                info = fence.group(1).split()
                fence_lang = info[0] if info else ""
                if markup and markup[-1] != _sep():
                    markup.append(_sep())
                if fence_lang:
                    markup.append(("md-dim", f"┌─ {fence_lang}"))
                    markup.append(_sep())
                continue
            in_fence = False
            if fence_lang:
                markup.append(("md-dim", "└─"))
                markup.append(_sep())
            continue
        if in_fence:
            markup.append(("md-code", f"│ {line}"))
            markup.append(_sep())
            continue

        if not line.strip():
            markup.append(_sep())
            continue
        if _HR_RE.match(line):
            markup.append(("md-dim", "─" * 40))
            markup.append(_sep())
            continue
        heading = _HEADING_RE.match(line.strip())
        if heading:
            style = {1: "md-h1", 2: "md-h2"}.get(len(heading.group(1)), "md-h3")
            markup.append((style, heading.group(2).strip()))
            markup.append(_sep())
            continue
        if _TABLE_ROW_RE.match(line):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if all(_CELL_SEP_RE.fullmatch(cell) for cell in cells):
                continue  # drop the alignment/separator row
            markup.append(("md-table", "  " + "  ·  ".join(cells)))
            markup.append(_sep())
            continue
        quote = _QUOTE_RE.match(line)
        if quote:
            markup.append(("md-quote", "│ "))
            markup.extend(_inline(quote.group(1), "md-quote"))
            markup.append(_sep())
            continue
        ordered = _OLIST_RE.match(line)
        if ordered:
            markup.append("  " * (len(ordered.group(1)) // 2))
            markup.append(("md-bullet", f"{ordered.group(2)}. "))
            markup.extend(_inline(ordered.group(3)))
            markup.append(_sep())
            continue
        unordered = _ULIST_RE.match(line)
        if unordered:
            markup.append("  " * (len(unordered.group(1)) // 2))
            markup.append(("md-bullet", "• "))
            markup.extend(_inline(unordered.group(2)))
            markup.append(_sep())
            continue

        markup.extend(_inline(line.strip()))
        markup.append(_sep())

    if markup and markup[-1] == _sep():
        markup.pop()
    return markup or [""]


# ------------------------------------------------------ event humanizing

def _clip(text: str, limit: int = 100) -> str:
    """One line, whitespace-collapsed, ellipsized to *limit* chars."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


_ARG_KEYS = (
    "command",
    "query",
    "url",
    "path",
    "file",
    "pattern",
    "text",
    "name",
    "topic",
)

_RESULT_KEYS = ("output", "result", "content", "summary", "message", "answer")


def humanize_tool_args(detail: str) -> str:
    """Make a tool-call payload readable: ``{"command": "git status"}`` → ``git status``."""
    try:
        parsed = json.loads(detail)
    except (TypeError, ValueError):
        return _clip(detail, 90)
    if isinstance(parsed, dict):
        for key in _ARG_KEYS:
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return _clip(value, 90)
        if not parsed:
            return ""
        pairs = "  ".join(f"{key}={_clip(str(value), 40)}" for key, value in list(parsed.items())[:2])
        return _clip(pairs, 90)
    if isinstance(parsed, list):
        return f"<{len(parsed)} item(s)>"
    return _clip(str(parsed), 90)


def humanize_tool_result(detail: str) -> str:
    """One readable line from a tool-result payload (JSON-aware)."""
    stripped = detail.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, str) and error.strip():
                return _clip(error)
            for key in _RESULT_KEYS:
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return _clip(value.strip().splitlines()[0])
            return _clip("keys: " + ", ".join(str(key) for key in list(parsed)[:6]))
        if isinstance(parsed, list):
            return f"<list of {len(parsed)} item(s)>"
        if parsed is not None:
            return _clip(str(parsed))
    first_line = stripped.splitlines()[0] if stripped else ""
    return _clip(first_line)


class UrwidRepl(Repl):
    """ARIA's urwid interface; inherits every /command from the legacy Repl."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._loop: urwid.MainLoop | None = None
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._turn_thread: threading.Thread | None = None
        self._listbox: urwid.ListBox | None = None
        self._prompt_edit: urwid.Edit | None = None
        self._plain_log: list[str] = []
        # Set when the UI thread dies from anything other than a clean stop;
        # run() re-raises it so the process exits non-zero or falls back (BR-3).
        self._ui_error: BaseException | None = None

    # ------------------------------------------------------------ plumbing
    def _post_ui(self, action: Callable[[], None]) -> None:
        """Run *action* on the UI thread from any thread.

        Called from the turn worker for every transcript/status update. When
        no loop is running (e.g. tests driving the repl directly) the action
        runs inline so behavior stays synchronous.
        """
        loop = self._async_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(action)
        else:
            action()

    def _quit(self) -> None:
        """Ask the urwid loop (from any thread) to stop."""
        loop = self._async_loop
        main_loop = self._loop
        if loop is not None and loop.is_running() and main_loop is not None:
            loop.call_soon_threadsafe(main_loop.stop)

    def _refresh(self) -> None:
        if self._loop is not None:
            try:
                self._loop.draw_screen()
            except AssertionError:
                pass  # draw_screen is illegal between event ticks; next tick repaints

    def _append_widget(self, widget: urwid.Widget) -> None:
        if self._listbox is None:
            return
        body = cast("list[urwid.Widget]", self._listbox.body)  # type: ignore[attr-defined]
        body.append(widget)
        self._listbox.focus_position = len(body) - 1  # type: ignore[attr-defined]
        self._refresh()

    def _timestamp(self) -> str:
        return datetime.now().strftime("%H:%M")

    @staticmethod
    def _plain(output: Any) -> str:
        if isinstance(output, urwid.Text):
            return str(output.get_text()[0])
        return str(output or "")

    # ------------------------------------------------------ transcript API
    def _remember_input(self, text: str) -> None:
        self._plain_log.append(f"[{self._timestamp()}] You: {text}")
        pile = urwid.Pile(
            [
                urwid.Text(("user-title", f"─ You ─ {self._timestamp()} ")),
                urwid.Text(("user", text)),
                urwid.Text(""),
            ]
        )
        self._append_widget(pile)

    def _remember_response(self, body: Any) -> None:
        if isinstance(body, urwid.Text):
            widget = body
        else:
            plain = body if isinstance(body, str) else str(body)
            self._plain_log.append(f"[{self._timestamp()}] ARIA: {plain}")
            widget = urwid.Text(markdown_to_markup(plain))
        header = urwid.Text(("aria-title", f"─ ARIA ─ {self._timestamp()} "))
        self._append_widget(urwid.Pile([header, widget, urwid.Text("")]))

    def _remember_command(self, command: str, output: Any = None) -> None:
        plain = self._plain(output)
        self._plain_log.append(f"[{self._timestamp()}] {command}: {plain}".rstrip())
        header = urwid.Text(("command-title", f"─ ⌘ {command} "))
        body = urwid.Text(("command", plain)) if plain else urwid.Text("")
        self._append_widget(urwid.Pile([header, body, urwid.Text("")]))

    def _error_panel(self, message: str) -> None:
        self._plain_log.append(f"[{self._timestamp()}] ERROR: {message}")
        self._append_widget(
            urwid.Pile(
                [
                    urwid.Text(("error-title", "─ Error ")),
                    urwid.Text(("error", message)),
                    urwid.Text(""),
                ]
            )
        )

    def _remember_trace(self, steps: list[list[Any]]) -> None:
        """Persist the finished execution trace into the transcript."""
        if not steps:
            return
        lines: list[Any] = [("trace-title", "─ Execution trace ")]
        for step in steps:
            lines.append(_sep())
            lines.extend(step)
        self._append_widget(urwid.Pile([urwid.Text(lines), urwid.Text("")]))
        self._refresh()

    # ------------------------------------------------------------ turn run
    def _run_turn(self, user_text: str) -> None:
        """Run one agent turn; called on the turn worker thread.

        Every UI mutation is marshaled through :meth:`_post_ui` so urwid's
        widgets are only touched from the UI thread. The streaming callbacks
        fire from this worker and repaint live through the asyncio loop, so
        the chain of thought and the partial reply update as they happen.
        """
        chunks: list[str] = []
        steps: list[list[Any]] = []
        coder_notes: list[str] = []

        live_header = urwid.Text(("aria-title", f"─ ARIA ─ {self._timestamp()} … "))
        live_body = urwid.Text("")
        live_pile = urwid.Pile([live_header, live_body])
        self._post_ui(lambda: self._append_widget(live_pile))

        def update() -> None:
            def paint() -> None:
                markup: list[Any] = []
                if self.show_cot and steps:
                    for step in steps:
                        markup.extend(step)
                        markup.append(_sep())
                    markup.append(_sep())
                markup.extend(markdown_to_markup("".join(chunks)) if chunks else ["- Working..."])
                if coder_notes:
                    markup.append(_sep())
                    markup.append(("coder-title", "─ ARIA · Coder "))
                    markup.append(_sep())
                    markup.append(("coder", coder_notes[-1]))
                live_body.set_text(markup)

            self._post_ui(paint)

        def on_text(text: str) -> None:
            chunks.append(text)
            update()

        def on_coder(text: str) -> None:
            if coder_notes:
                coder_notes[-1] += text
            else:
                coder_notes.append(text)
            update()

        def on_event(event: Any) -> None:
            if not self.show_cot:
                return
            line = self._render_event_markup(event)
            if line is not None:
                steps.append(line)
                update()

        if self._deploy_handler:
            self._deploy_handler.set_stream_hook(on_coder, on_event)
        try:
            try:
                result = self.agent.run_with_attachments(user_text, on_text=on_text, on_event=on_event)
                update()
            except KeyboardInterrupt:

                def interrupted() -> None:
                    self._remove_live(live_pile)
                    self._remember_response("Interrupted.")

                self._post_ui(interrupted)
                return
        finally:
            if self._deploy_handler:
                self._deploy_handler.set_stream_hook(None, None)

        def finish() -> None:
            self._remove_live(live_pile)
            if self.keep_cot and steps:
                self._remember_trace(steps)
            self._remember_response(result)

        self._post_ui(finish)

        if self.speech:
            self.speech.say(result)
            if self.speech.last_error:
                message = f"Speech unavailable: {self.speech.last_error}"
                self._post_ui(lambda: self._remember_command("/tts", message))

    def _remove_live(self, live_pile: urwid.Widget) -> None:
        if self._listbox is None:
            return
        body = cast("list[urwid.Widget]", self._listbox.body)  # type: ignore[attr-defined]
        if live_pile in body:
            body[body.index(live_pile)] = urwid.Text("")

    def _render_event_markup(self, event: Any) -> list[Any] | None:
        """One human-readable execution-trace line for an agent event.

        Tool payloads arrive as JSON (the wire format); render them as
        short, readable lines instead of dumping the raw objects.
        """
        if event.kind == "round":
            return [("trace-dim", f"◦ round {event.round}")]
        if event.kind == "tool_call":
            args = humanize_tool_args(event.detail)
            line: list[Any] = [("trace", "⚙ "), ("trace-tool", event.name)]
            if args:
                line.append(("trace", f"  {args}"))
            return line
        if event.kind == "tool_result":
            mark, mark_style = ("✓", "trace-ok") if event.ok else ("✗", "trace-fail")
            shown = humanize_tool_result(event.detail)
            return [
                (mark_style.replace("cot", "trace"), f"{mark} "),
                ("trace-tool", event.name),
                ("trace-dim", f"  {shown}"),
            ]
        if event.kind == "limit":
            return [("trace-warn", f"⚠ {event.detail}")]
        return None

    # -------------------------------------------------------------- export
    def _cmd_save(self, argument: str) -> None:
        target = (
            Path(argument).expanduser()
            if argument
            else Path(f"aria-session-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt")
        )
        try:
            target.write_text("\n".join(self._plain_log) + "\n", encoding="utf-8")
            self._remember_command("/save", f"Session exported to {target.resolve()}")
        except OSError as exc:
            self._error_panel(f"Could not save session: {exc}")

    # ----------------------------------------------------------- main loop
    def _header(self) -> urwid.Widget:
        lines: list[Any] = []
        for line in _LOGO_LINES:
            lines.append(("logo", line.rstrip()))
            lines.append(_sep())
        lines.append(("tagline", "ADAPTIVE REASONING & INTELLIGENCE ASSISTANT"))
        lines.append(_sep())
        lines.append(("stamp", f"{datetime.now():%a %d %b %Y}  ·  ready"))
        return urwid.Text(lines, align="center")

    def _status_markup(self) -> list[Any]:
        model = self.agent.model_name or "unknown model"
        speech = "TTS on" if self.speech and self.speech.enabled else "TTS off"
        cot = "Trace on" if self.show_cot else "Trace off"
        if self.keep_cot:
            cot += "+keep"
        return [
            ("status", "● ONLINE"),
            ("status-dim", "  │  "),
            ("status", model),
            ("status-dim", "  │  "),
            ("status-dim", speech),
            ("status-dim", "  │  "),
            ("status-dim", cot),
            ("status-dim", "  │  /help"),
        ]

    def _submit(self) -> None:
        edit = self._prompt_edit
        if edit is None:
            return
        user_text = edit.get_edit_text().strip()
        if not user_text:
            return
        edit.set_edit_text("")
        self._dispatch(user_text)

    def _history_older(self) -> None:
        edit = self._prompt_edit
        if edit is None or not self._input_history:
            return
        if self._history_index is None:
            self._history_index = len(self._input_history)
        self._history_index = max(0, self._history_index - 1)
        edit.set_edit_text(self._input_history[self._history_index])
        edit.set_edit_pos(len(edit.get_edit_text()))

    def _history_newer(self) -> None:
        edit = self._prompt_edit
        if edit is None or self._history_index is None:
            return
        self._history_index += 1
        if self._history_index >= len(self._input_history):
            self._history_index = None
            edit.set_edit_text("")
        else:
            edit.set_edit_text(self._input_history[self._history_index])
            edit.set_edit_pos(len(edit.get_edit_text()))

    def run(self) -> None:
        prompt_edit = urwid.Edit(("edit-caption", "You › "), "")
        self._prompt_edit = prompt_edit
        status = urwid.Text(self._status_markup())
        footer = urwid.AttrMap(
            urwid.Text("Enter send · PgUp/PgDn or mouse wheel scroll · /help commands · Ctrl+C quit"),
            "footer",
        )

        walker = urwid.SimpleFocusListWalker([])  # type: ignore[var-annotated]
        listbox = urwid.ListBox(cast("Any", walker))
        self._listbox = listbox

        frame = urwid.Frame(
            body=cast("Any", listbox),
            header=cast("Any", urwid.Pile([self._header(), status, urwid.Text("")])),
            footer=cast(
                "Any", urwid.Pile([urwid.Text(""), urwid.AttrMap(prompt_edit, "edit"), footer])
            ),
            focus_part="footer",
        )

        def unhandled(key: str | tuple[str, int, int, int]) -> bool | None:
            if key == "enter":
                self._submit()
            return None

        original_edit_keypress = prompt_edit.keypress

        def on_edit_keypress(size: Any, key: str) -> str | None:
            if key == "enter":
                self._submit()
                return None
            if key == "up" and not prompt_edit.get_edit_text():
                self._history_older()
                return None
            if key == "down" and not prompt_edit.get_edit_text():
                self._history_newer()
                return None
            return original_edit_keypress(size, key)  # type: ignore[arg-type]

        prompt_edit.keypress = on_edit_keypress  # type: ignore[method-assign]

        async_loop = asyncio.new_event_loop()
        self._async_loop = async_loop
        loop = urwid.MainLoop(
            frame,
            palette=PALETTE,
            handle_mouse=True,
            unhandled_input=unhandled,
            event_loop=urwid.AsyncioEventLoop(loop=async_loop),
        )
        self._loop = loop

        # urwid's terminal setup installs signal handlers, which Python only
        # permits from the main thread. Keep the UI loop on the calling thread;
        # agent turns remain on worker threads and marshal their updates here.
        self._ui_error = None
        try:
            asyncio.set_event_loop(async_loop)
            loop.run()
        except KeyboardInterrupt:  # pragma: no cover - Ctrl+C is terminal input
            pass
        except BaseException as exc:
            # BR-3: record the crash so the application can fall back to Rich
            # instead of treating a failed TUI as a clean shutdown.
            self._ui_error = exc
            log_error(f"UrwidRepl: UI thread crashed: {type(exc).__name__}: {exc}")
        finally:
            self._loop = None
            try:
                async_loop.close()
            except Exception as close_error:  # noqa: BLE001 - shutdown path
                log_error(f"UrwidRepl: async loop close failed: {close_error}")
            self._async_loop = None

        if self._ui_error is not None:
            raise self._ui_error

    # --------------------------------------------------------- dispatcher
    def _dispatch(self, user_text: str) -> None:
        """Mirror Repl.run's input handling for one submitted line."""
        if not self._input_history or self._input_history[-1] != user_text:
            self._input_history.append(user_text)
        self._input_history = self._input_history[-100:]
        if user_text.lower() in {"/quit", "/exit"}:
            raise urwid.ExitMainLoop()
        if user_text.startswith("/"):
            self._handle_command(user_text)
            return
        self._remember_input(user_text)
        if self._turn_thread is not None and self._turn_thread.is_alive():
            self._error_panel("ARIA is still answering the previous message.")
            return
        try:
            # Skills are re-read every turn so edits apply without a restart.
            refresh_skills = getattr(self.agent, "refresh_skills", None)
            if callable(refresh_skills):
                refresh_skills()
        except Exception as exc:
            log_error(f"Repl: skills refresh failed: {type(exc).__name__}: {exc}")
        thread = threading.Thread(target=self._turn_job, args=(user_text,), name="aria-turn", daemon=True)
        self._turn_thread = thread
        thread.start()

    def _turn_job(self, user_text: str) -> None:
        """Worker-thread entry point for one agent turn."""
        try:
            self._run_turn(user_text)
        except Exception as exc:
            from ..logging_setup import log_error

            log_error(f"Repl: turn failed: {type(exc).__name__}: {exc}")
            message = f"{type(exc).__name__}: {exc}"
            self._post_ui(lambda: self._error_panel(message))
