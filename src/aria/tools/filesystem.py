"""Workspace filesystem tools, including bulk operations for the coder agent."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from ..logging.setup import log_debug, log_error
from .base import Tool, ToolContext, ToolResult
from .registry import ToolRegistry

IGNORED_DIRECTORIES = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache"}


def _path(arguments: dict[str, Any], context: ToolContext) -> Path:
    raw = arguments.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path must be a non-empty string")
    candidate = (context.workspace / raw).resolve()
    try:
        candidate.relative_to(context.workspace)
    except ValueError as exc:
        log_error(f"Filesystem: path escaped workspace: {raw}")
        raise ValueError("path must remain inside the workspace") from exc
    return candidate


def _list_directory(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    directory = _path(arguments, context)
    if not directory.is_dir():
        return ToolResult(f"Not a directory: {arguments['path']}", is_error=True)
    entries = []
    for entry in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        suffix = "/" if entry.is_dir() else ""
        entries.append(f"{entry.name}{suffix}")
    return ToolResult("\n".join(entries) or "(empty directory)")


def _read_file(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    file_path = _path(arguments, context)
    if not file_path.is_file():
        return ToolResult(f"Not a file: {arguments['path']}", is_error=True)
    encoding = arguments.get("encoding", "utf-8")
    try:
        return ToolResult(file_path.read_text(encoding=encoding))
    except UnicodeDecodeError as exc:
        return ToolResult(f"Could not decode file as {encoding}: {exc}", is_error=True)


def _write_file(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    file_path = _path(arguments, context)
    content = arguments.get("content")
    if not isinstance(content, str):
        return ToolResult("content must be a string", is_error=True)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    log_debug(f"Filesystem: wrote {file_path} ({len(content)} chars)")
    return ToolResult(f"Wrote {file_path.relative_to(context.workspace)}")


def _edit_file(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    file_path = _path(arguments, context)
    old = arguments.get("old_text")
    new = arguments.get("new_text")
    if not isinstance(old, str) or not old:
        return ToolResult("old_text must be a non-empty string", is_error=True)
    if not isinstance(new, str):
        return ToolResult("new_text must be a string", is_error=True)
    if not file_path.is_file():
        return ToolResult(f"Not a file: {arguments['path']}", is_error=True)
    content = file_path.read_text(encoding="utf-8")
    occurrences = content.count(old)
    if occurrences == 0:
        return ToolResult("old_text was not found in the file", is_error=True)
    if occurrences > 1 and not arguments.get("replace_all", False):
        return ToolResult(
            f"old_text occurs {occurrences} times; set replace_all=true to edit all occurrences",
            is_error=True,
        )
    replacement_count = occurrences if arguments.get("replace_all", False) else 1
    file_path.write_text(content.replace(old, new, replacement_count), encoding="utf-8")
    log_debug(f"Filesystem: edited {file_path} ({replacement_count} replacement(s))")
    return ToolResult(f"Edited {file_path.relative_to(context.workspace)}")


def _search_workspace(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    """Grep-like search: pattern plus optional glob filter, capped results."""
    pattern = arguments.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        return ToolResult("pattern must be a non-empty string", is_error=True)
    glob = arguments.get("glob")
    if glob is not None and not isinstance(glob, str):
        return ToolResult("glob must be a string like '*.py'", is_error=True)
    max_results = arguments.get("max_results", 100)
    if not isinstance(max_results, int) or max_results <= 0:
        max_results = 100

    matches: list[str] = []
    truncated = False
    for file_path in sorted(context.workspace.rglob("*" if glob is None else glob)):
        if not file_path.is_file():
            continue
        if any(part in IGNORED_DIRECTORIES for part in file_path.parts):
            continue
        try:
            for line_number, line in enumerate(
                file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
            ):
                if pattern in line:
                    matches.append(f"{file_path.relative_to(context.workspace)}:{line_number}: {line.strip()}")
                    if len(matches) >= max_results:
                        truncated = True
                        break
        except OSError:
            continue
        if truncated:
            break
    if not matches:
        return ToolResult(f"No matches for {pattern!r}")
    summary = "\n".join(matches)
    if truncated:
        summary += f"\n[stopped after {max_results} matches]"
    return ToolResult(summary)


def _apply_edits(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
    """Batch edit one file in a single call - the coder agent's bulk tool."""
    file_path = _path(arguments, context)
    edits = arguments.get("edits")
    if not isinstance(edits, list) or not edits:
        return ToolResult("edits must be a non-empty list of {old_text, new_text}", is_error=True)
    if not file_path.is_file():
        return ToolResult(f"Not a file: {arguments['path']}", is_error=True)

    content = file_path.read_text(encoding="utf-8")
    applied = 0
    failures: list[str] = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            failures.append(f"edit {index}: not an object")
            continue
        old = edit.get("old_text")
        new = edit.get("new_text", "")
        if not isinstance(old, str) or not old:
            failures.append(f"edit {index}: old_text must be a non-empty string")
            continue
        occurrences = content.count(old)
        if occurrences == 0:
            failures.append(f"edit {index}: old_text not found")
            continue
        replace_all = bool(edit.get("replace_all", False))
        if occurrences > 1 and not replace_all:
            failures.append(f"edit {index}: old_text occurs {occurrences} times; needs replace_all")
            continue
        count = occurrences if replace_all else 1
        content = content.replace(old, new, count)
        applied += 1

    if applied:
        file_path.write_text(content, encoding="utf-8")
        log_debug(f"Filesystem: batch_edit on {file_path}: {applied} applied")
    report = f"applied {applied}/{len(edits)} edits"
    if failures:
        report += "\nfailed edits:\n" + "\n".join(failures)
    return ToolResult(report, is_error=bool(failures and not applied))


def register_filesystem_tools(registry: ToolRegistry) -> None:
    path_parameter = {"type": "string", "description": "Path relative to the workspace"}
    registry.register(
        Tool(
            name="list_directory",
            description="List files and subdirectories at a workspace-relative path.",
            parameters={
                "type": "object",
                "properties": {"path": path_parameter},
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=_list_directory,
        )
    )
    registry.register(
        Tool(
            name="read_file",
            description="Read a UTF-8 text file at a workspace-relative path.",
            parameters={
                "type": "object",
                "properties": {"path": path_parameter},
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=_read_file,
        )
    )
    registry.register(
        Tool(
            name="write_file",
            description="Create or overwrite a UTF-8 text file at a workspace-relative path.",
            parameters={
                "type": "object",
                "properties": {"path": path_parameter, "content": {"type": "string"}},
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=_write_file,
        )
    )
    registry.register(
        Tool(
            name="edit_file",
            description="Replace exact text in an existing UTF-8 text file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": path_parameter,
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
            handler=_edit_file,
        )
    )
    registry.register(
        Tool(
            name="search_workspace",
            description=(
                "Search all workspace text files for a substring, returning file:line matches. "
                "Optional glob filter such as '*.py'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string"},
                    "max_results": {"type": "integer", "default": 100},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=_search_workspace,
        )
    )
    registry.register(
        Tool(
            name="batch_edit",
            description=(
                "Apply multiple exact-text edits to one file in a single call. Each edit is an "
                "object with old_text, new_text and optional replace_all. Efficient for large changes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": path_parameter,
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"},
                                "replace_all": {"type": "boolean"},
                            },
                            "required": ["old_text", "new_text"],
                        },
                    },
                },
                "required": ["path", "edits"],
                "additionalProperties": False,
            },
            handler=_apply_edits,
        )
    )


def register_filesystem_tools_with_glob(registry: ToolRegistry) -> None:  # pragma: no cover
    """Kept for API symmetry; registration happens in register_filesystem_tools."""
    register_filesystem_tools(registry)
