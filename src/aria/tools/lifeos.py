"""LifeOS connection: manage the user's personal life through the LifeOS API.

Ported from CIEL's LifeOS client (backend/tests/legacy/lifeOS.py) into ARIA's
tool architecture: the wire behavior is unchanged (Bearer auth, idempotency
keys, retry with backoff on 5xx/timeouts), but the client is config-driven and
exposed as a single registered tool covering every read and write operation.
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..config import LifeOSConfig
from ..logging.setup import log_debug, log_error, log_info
from .base import Tool, ToolContext, ToolResult
from .registry import ToolRegistry

READ_OPERATIONS = {
    "get_capabilities": ("GET", "/capabilities"),
    "get_today": ("GET", "/context/today"),
    "get_weekly_review": ("GET", "/context/weekly-review"),
    "search": ("GET", "/search"),
    "list_tasks": ("GET", "/tasks"),
    "get_task": ("GET", "/tasks/{task_id}"),
    "list_projects": ("GET", "/projects"),
    "get_project": ("GET", "/projects/{project_id}"),
    "list_goals": ("GET", "/goals"),
    "list_habits": ("GET", "/habits"),
    "list_calendar": ("GET", "/calendar"),
    "list_notes": ("GET", "/notes"),
    "list_library": ("GET", "/library/items"),
    "list_contacts": ("GET", "/contacts"),
    "list_journal": ("GET", "/journal"),
    "list_health": ("GET", "/health"),
    "list_diet": ("GET", "/diet"),
    "list_gym_routines": ("GET", "/gym/routines"),
    "list_gym_logs": ("GET", "/gym/logs"),
    "list_finance": ("GET", "/finance"),
    "list_events": ("GET", "/events"),
}

WRITE_OPERATIONS = {
    "create_task": ("POST", "/tasks"),
    "update_task": ("PATCH", "/tasks/{task_id}"),
    "complete_task": ("PATCH", "/tasks/{task_id}"),
    "create_project": ("POST", "/projects"),
    "update_project": ("PATCH", "/projects/{project_id}"),
    "create_project_milestone": ("POST", "/projects/{project_id}/milestones"),
    "update_project_milestone": ("PATCH", "/projects/{project_id}/milestones/{milestone_id}"),
    "create_goal": ("POST", "/goals"),
    "update_goal": ("PATCH", "/goals/{goal_id}"),
    "create_goal_milestone": ("POST", "/goals/{goal_id}/milestones"),
    "update_goal_milestone": ("PATCH", "/goals/{goal_id}/milestones/{milestone_id}"),
    "create_habit": ("POST", "/habits"),
    "update_habit": ("PATCH", "/habits/{habit_id}"),
    "log_habit": ("POST", "/habits/{habit_id}/logs"),
    "create_calendar_event": ("POST", "/calendar/events"),
    "update_calendar_event": ("PATCH", "/calendar/events/{event_id}"),
    "create_note": ("POST", "/notes"),
    "update_note": ("PATCH", "/notes/{note_id}"),
    "create_library_item": ("POST", "/library/items"),
    "update_library_item": ("PATCH", "/library/items/{item_id}"),
    "create_contact": ("POST", "/contacts"),
    "update_contact": ("PATCH", "/contacts/{contact_id}"),
    "create_journal": ("POST", "/journal"),
    "update_journal": ("PATCH", "/journal/{entry_id}"),
    "create_health": ("POST", "/health"),
    "create_diet": ("POST", "/diet"),
    "create_gym_log": ("POST", "/gym/logs"),
    "create_finance": ("POST", "/finance"),
    "update_finance": ("PATCH", "/finance/{entry_id}"),
    "acknowledge_event": ("POST", "/events/{event_id}/acknowledge"),
}

LIFEOS_OPERATIONS = {**READ_OPERATIONS, **WRITE_OPERATIONS}

PATH_ARGUMENTS = {
    "task_id",
    "project_id",
    "goal_id",
    "habit_id",
    "event_id",
    "note_id",
    "item_id",
    "contact_id",
    "entry_id",
    "milestone_id",
}


def resolve_api_key(config: LifeOSConfig) -> str:
    """The LifeOS key lives in .env under the configured variable name."""
    return str(os.environ.get(config.api_key_env, "") or "").strip()


def is_configured(config: LifeOSConfig) -> bool:
    return bool(config.base_url.strip() and resolve_api_key(config))


def _format_path(path_template: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    remaining = dict(arguments)
    path_values: dict[str, int] = {}
    for field in PATH_ARGUMENTS:
        placeholder = "{" + field + "}"
        if placeholder not in path_template:
            continue
        value = remaining.pop(field, None)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{field} must be a positive integer")
        path_values[field] = value
    return path_template.format(**path_values), remaining


def _decode_response(raw: bytes) -> Any:
    if not raw:
        return {}
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"message": text}


def run_lifeos_action(config: LifeOSConfig, operation: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute one LifeOS operation and return a serializable result dict."""
    if not config.enabled:
        return {"success": False, "statusCode": 0, "error": "LifeOS is disabled (lifeos.enabled: false)."}
    if not is_configured(config):
        return {
            "success": False,
            "statusCode": 0,
            "error": (
                "LifeOS is not configured. Set lifeos.base_url in config.yaml and "
                f"{config.api_key_env} in .env."
            ),
        }
    if operation not in LIFEOS_OPERATIONS:
        return {
            "success": False,
            "statusCode": 0,
            "error": f"Unknown LifeOS operation: {operation}",
            "availableOperations": sorted(LIFEOS_OPERATIONS),
        }
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return {"success": False, "statusCode": 0, "error": "LifeOS arguments must be an object."}

    method, path_template = LIFEOS_OPERATIONS[operation]
    try:
        path, request_arguments = _format_path(path_template, arguments)
    except ValueError as error:
        return {"success": False, "statusCode": 0, "error": str(error)}

    if operation == "complete_task":
        request_arguments["status"] = "completed"

    idempotency_key = str(request_arguments.pop("idempotency_key", "") or uuid.uuid4().hex)
    url = f"{config.base_url.rstrip('/')}/api/v1/assistant{path}"
    body = None
    if method == "GET" and request_arguments:
        url = f"{url}?{urlencode(request_arguments, doseq=True)}"
    elif method != "GET":
        body = json.dumps(request_arguments).encode("utf-8")

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {resolve_api_key(config)}",
        "User-Agent": "ARIA-LifeOS/1.0",
        "X-Request-ID": uuid.uuid4().hex,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Idempotency-Key"] = idempotency_key

    attempts = max(0, int(config.max_retries)) + 1
    for attempt in range(attempts):
        request_object = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request_object, timeout=float(config.timeout_seconds)) as response:
                data = _decode_response(response.read())
                log_debug(f"LifeOS: {operation} -> {response.status}")
                return {"success": True, "statusCode": response.status, "data": data}
        except HTTPError as error:
            data = _decode_response(error.read())
            if error.code in {502, 503, 504} and attempt + 1 < attempts:
                time.sleep(float(config.retry_backoff_seconds) * (2**attempt))
                continue
            if isinstance(data, dict):
                message = data.get("message") or data.get("error")
            else:
                message = str(data)
            log_error(f"LifeOS: {operation} -> HTTP {error.code}: {message}")
            return {"success": False, "statusCode": error.code, "error": message, "data": data}
        except (URLError, TimeoutError, socket.timeout, OSError) as error:
            if attempt + 1 < attempts:
                time.sleep(float(config.retry_backoff_seconds) * (2**attempt))
                continue
            log_error(f"LifeOS: {operation} -> {type(error).__name__}: {error}")
            return {"success": False, "statusCode": 0, "error": f"LifeOS request failed: {error}"}

    return {"success": False, "statusCode": 0, "error": "LifeOS request failed."}


class LifeOSTool:
    """Handler binding the LifeOS client to its config, like DeployCoderTool."""

    def __init__(self, config: LifeOSConfig) -> None:
        self._config = config

    def __call__(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        operation = arguments.get("operation")
        if not isinstance(operation, str) or not operation.strip():
            return ToolResult("operation must be a LifeOS operation name", is_error=True)
        tool_arguments = arguments.get("arguments", {})
        if not isinstance(tool_arguments, dict):
            return ToolResult("arguments must be an object", is_error=True)

        log_info(f"LifeOS: {operation} with {len(tool_arguments)} argument(s)")
        result = run_lifeos_action(self._config, operation, tool_arguments)
        return ToolResult(json.dumps(result), is_error=not result.get("success", False))


def register_lifeos_tool(registry: ToolRegistry, config: LifeOSConfig) -> LifeOSTool:
    """Register the single LifeOS tool covering every read/write operation."""
    handler = LifeOSTool(config)
    registry.register(
        Tool(
            name="lifeos",
            description=(
                "Manage the user's personal life in LifeOS: tasks, projects, goals, habits, "
                "calendar, notes, library, contacts, journal, health, diet, gym, finance, "
                "events, plus context summaries (get_today, get_weekly_review, search). "
                "Pass 'operation' and 'arguments': path ids (task_id, project_id, goal_id, "
                "habit_id, event_id, note_id, item_id, contact_id, entry_id, milestone_id) as "
                "positive integers, query filters for reads, or body fields for writes. "
                "complete_task marks the task completed automatically. Write calls carry an "
                "auto idempotency key; pass arguments.idempotency_key to keep logical retries "
                "from duplicating data."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "enum": sorted(LIFEOS_OPERATIONS)},
                    "arguments": {"type": "object", "additionalProperties": True},
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
            handler=handler,
        )
    )
    return handler
