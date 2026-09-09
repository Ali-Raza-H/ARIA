"""Persistent Playwright browser tools for ARIA.

The browser is intentionally stateful: a single persistent Chromium context is
created on first use and kept for the process lifetime. This supports normal
interactive browsing, cookies, downloads, and uploads without leaking browser
objects into the coder agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ...config import BrowserConfig
from ...logging.setup import log_error, log_info
from ..core.base import Tool, ToolContext, ToolResult
from ..core.registry import ToolRegistry


class BrowserToolService:
    """Own a persistent Playwright Chromium context and expose small actions."""

    def __init__(self, config: BrowserConfig, workspace: Path) -> None:
        self.config = config
        self.workspace = workspace
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None

    def _ensure_page(self) -> Any:
        if self._page is not None and not self._page.is_closed():
            return self._page
        if self._context is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise RuntimeError(
                    "Playwright is unavailable. Install project dependencies and run "
                    "'playwright install chromium'."
                ) from exc
            self.config.profile_directory.mkdir(parents=True, exist_ok=True)
            self._playwright = sync_playwright().start()
            launch_args = list(self.config.launch_args)
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.config.profile_directory),
                headless=self.config.headless,
                executable_path=self.config.executable_path or None,
                args=launch_args,
                viewport={"width": self.config.viewport_width, "height": self.config.viewport_height},
            )
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        return self._page

    @staticmethod
    def _selector(arguments: dict[str, Any]) -> str:
        selector = arguments.get("selector")
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError("selector must be a non-empty CSS/text selector")
        return selector.strip()

    def navigate(self, arguments: dict[str, Any]) -> str:
        url = arguments.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty URL")
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https", "file"}:
            raise ValueError("browser navigation supports http, https, and file URLs")
        page = self._ensure_page()
        response = page.goto(url.strip(), wait_until=self.config.wait_until, timeout=self.config.timeout_ms)
        status_value = getattr(response, "status", None) if response is not None else None
        status = status_value() if callable(status_value) else status_value
        log_info(f"browser.navigate url={url[:200]!r} status={status}")
        return json.dumps({"url": page.url, "title": page.title(), "status": status})

    def snapshot(self, _arguments: dict[str, Any]) -> str:
        page = self._ensure_page()
        text = page.locator("body").inner_text(timeout=self.config.timeout_ms)
        return json.dumps({"url": page.url, "title": page.title(), "text": text[: self.config.max_text_chars]})

    def click(self, arguments: dict[str, Any]) -> str:
        page = self._ensure_page()
        selector = self._selector(arguments)
        page.locator(selector).click(timeout=self.config.timeout_ms)
        return json.dumps({"clicked": selector, "url": page.url, "title": page.title()})

    def type_text(self, arguments: dict[str, Any]) -> str:
        page = self._ensure_page()
        selector = self._selector(arguments)
        text = arguments.get("text")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        page.locator(selector).fill(text, timeout=self.config.timeout_ms)
        return json.dumps({"typed": len(text), "selector": selector})

    def press(self, arguments: dict[str, Any]) -> str:
        page = self._ensure_page()
        key = arguments.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("key must be a non-empty Playwright key name")
        selector = arguments.get("selector")
        if selector is not None and not isinstance(selector, str):
            raise ValueError("selector must be a string when provided")
        target = page.locator(selector) if selector else page
        target.press(key.strip(), timeout=self.config.timeout_ms)
        return json.dumps({"pressed": key.strip(), "selector": selector})

    def tabs(self, _arguments: dict[str, Any]) -> str:
        context = self._context
        if context is None:
            return "[]"
        return json.dumps([
            {"index": index, "url": page.url, "title": page.title(), "active": page is self._page}
            for index, page in enumerate(context.pages)
            if not page.is_closed()
        ])

    def new_tab(self, _arguments: dict[str, Any]) -> str:
        if self._context is None:
            self._ensure_page()
        assert self._context is not None
        self._page = self._context.new_page()
        return json.dumps({"url": self._page.url, "title": self._page.title()})

    def select_tab(self, arguments: dict[str, Any]) -> str:
        index = arguments.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("index must be an integer")
        if self._context is None:
            self._ensure_page()
        assert self._context is not None
        pages = [page for page in self._context.pages if not page.is_closed()]
        if index < 0 or index >= len(pages):
            raise ValueError(f"tab index must be between 0 and {len(pages) - 1}")
        self._page = pages[index]
        return json.dumps({"index": index, "url": self._page.url, "title": self._page.title()})

    def close_tab(self, _arguments: dict[str, Any]) -> str:
        page = self._ensure_page()
        page.close()
        self._page = None
        return "closed current browser tab"

    def screenshot(self, arguments: dict[str, Any], context: ToolContext) -> str:
        page = self._ensure_page()
        path = self._output_path(arguments.get("path"), context, "browser.png")
        page.screenshot(path=str(path), full_page=bool(arguments.get("full_page", False)))
        return json.dumps({"path": str(path), "url": page.url})

    def download(self, arguments: dict[str, Any], context: ToolContext) -> str:
        page = self._ensure_page()
        selector = self._selector(arguments)
        path = self._output_path(arguments.get("path"), context, "download")
        with page.expect_download(timeout=self.config.timeout_ms) as download_info:
            page.locator(selector).click(timeout=self.config.timeout_ms)
        download = download_info.value
        download.save_as(str(path))
        return json.dumps({"path": str(path), "suggested_filename": download.suggested_filename})

    def upload(self, arguments: dict[str, Any], context: ToolContext) -> str:
        page = self._ensure_page()
        selector = self._selector(arguments)
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("path must be a non-empty file path")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = context.workspace / path
        path = path.resolve()
        if not path.is_file():
            raise ValueError(f"upload file does not exist: {path}")
        page.locator(selector).set_input_files(str(path), timeout=self.config.timeout_ms)
        return json.dumps({"uploaded": str(path), "selector": selector})

    @staticmethod
    def _output_path(raw: Any, context: ToolContext, default_name: str) -> Path:
        if raw is None:
            path = context.workspace / default_name
        elif isinstance(raw, str) and raw.strip():
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = context.workspace / path
        else:
            raise ValueError("path must be a non-empty path when provided")
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def close(self) -> None:
        """Close the persistent browser context, if one was started."""
        try:
            if self._context is not None:
                self._context.close()
        finally:
            self._context = None
            self._page = None
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None


def register_browser_tools(registry: ToolRegistry, service: BrowserToolService) -> None:
    """Register the full browser toolset in the caller's registry."""

    def call(method: str, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            if method in {"screenshot", "download"}:
                output = getattr(service, method)(arguments, context)
            else:
                output = getattr(service, method)(arguments)
            return ToolResult(output)
        except Exception as exc:
            log_error(f"browser.{method} failed: {type(exc).__name__}: {exc}")
            return ToolResult(f"Browser {method} failed: {type(exc).__name__}: {exc}", is_error=True)

    common = {"type": "object", "additionalProperties": False}
    registry.register(Tool(
        "browser_navigate", "Navigate the persistent Chromium browser to an HTTP(S) or local file URL.",
        {**common, "properties": {"url": {"type": "string"}}, "required": ["url"]},
        lambda args, _ctx: call("navigate", args, _ctx),
    ))
    registry.register(Tool(
        "browser_snapshot", "Read the visible page title and text from the current browser page.",
        {**common, "properties": {}}, lambda args, _ctx: call("snapshot", args, _ctx),
    ))
    registry.register(Tool(
        "browser_tabs", "List open persistent browser tabs and identify the active tab.",
        {**common, "properties": {}}, lambda args, _ctx: call("tabs", args, _ctx),
    ))
    registry.register(Tool(
        "browser_new_tab", "Open and select a new persistent browser tab.",
        {**common, "properties": {}}, lambda args, _ctx: call("new_tab", args, _ctx),
    ))
    registry.register(Tool(
        "browser_select_tab", "Select a persistent browser tab by its current index.",
        {**common, "properties": {"index": {"type": "integer", "minimum": 0}}, "required": ["index"]},
        lambda args, _ctx: call("select_tab", args, _ctx),
    ))
    registry.register(Tool(
        "browser_close_tab", "Close the current persistent browser tab.",
        {**common, "properties": {}}, lambda args, _ctx: call("close_tab", args, _ctx),
    ))
    registry.register(Tool(
        "browser_click", "Click an element in the current page using a Playwright selector.",
        {**common, "properties": {"selector": {"type": "string"}}, "required": ["selector"]},
        lambda args, _ctx: call("click", args, _ctx),
    ))
    registry.register(Tool(
        "browser_type", "Fill text into a form element in the current page.",
        {**common, "properties": {"selector": {"type": "string"}, "text": {"type": "string"}}, "required": ["selector", "text"]},
        lambda args, _ctx: call("type_text", args, _ctx),
    ))
    registry.register(Tool(
        "browser_press", "Press a Playwright keyboard key, optionally targeted at a selector.",
        {**common, "properties": {"key": {"type": "string"}, "selector": {"type": "string"}}, "required": ["key"]},
        lambda args, _ctx: call("press", args, _ctx),
    ))
    registry.register(Tool(
        "browser_screenshot", "Capture the current page to a PNG file.",
        {**common, "properties": {"path": {"type": "string"}, "full_page": {"type": "boolean"}}},
        lambda args, ctx: call("screenshot", args, ctx),
    ))
    registry.register(Tool(
        "browser_download", "Click a download control and save the resulting file.",
        {**common, "properties": {"selector": {"type": "string"}, "path": {"type": "string"}}, "required": ["selector"]},
        lambda args, ctx: call("download", args, ctx),
    ))
    registry.register(Tool(
        "browser_upload", "Upload a local file to a file-input element on the current page.",
        {**common, "properties": {"selector": {"type": "string"}, "path": {"type": "string"}}, "required": ["selector", "path"]},
        lambda args, ctx: call("upload", args, ctx),
    ))
