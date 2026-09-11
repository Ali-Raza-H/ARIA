"""Gmail tools backed by Google's official MCP and Gmail REST APIs.

Google's Gmail MCP is the preferred path for its currently supported preview
operations. The direct API client is intentionally explicit and is used only
for operations the MCP does not expose. OAuth tokens are read from configured
environment variables or a local token JSON file; credentials never enter tool
results or logs.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx

from ..config import GmailConfig
from ..logging.setup import log_error, log_info
from .core.base import Tool, ToolContext, ToolResult
from .core.confirmation import ConfirmationManager
from .core.registry import ToolRegistry


class _TokenSource:
    def __init__(self, config: GmailConfig) -> None:
        self.config = config

    def get(self, env_name: str) -> str:
        import os

        token = os.getenv(env_name, "").strip()
        if token:
            return token
        path = self.config.oauth_token_path
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    token = str(value.get("access_token", value.get("token", ""))).strip()
                    if token:
                        return token
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError(f"invalid Gmail OAuth token file: {path}: {exc}") from exc
        raise RuntimeError(
            f"Gmail authentication is not configured; set {env_name} or create "
            f"{path} with an OAuth access_token"
        )


class GmailMCPClient:
    """Small JSON-RPC-over-HTTP client for Google's Gmail MCP endpoint."""

    def __init__(self, config: GmailConfig) -> None:
        self.config = config
        self.tokens = _TokenSource(config)

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        response = httpx.post(
            self.config.mcp_url,
            headers={
                "Authorization": f"Bearer {self.tokens.get(self.config.access_token_env)}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json=payload,
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        return self._decode_response(response.text)

    @staticmethod
    def _decode_response(text: str) -> Any:
        stripped = text.strip()
        if stripped.startswith("data:"):
            values = []
            for line in stripped.splitlines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    if raw and raw != "[DONE]":
                        values.append(json.loads(raw))
            value = values[-1] if values else {}
        else:
            value = json.loads(stripped or "{}")
        if isinstance(value, dict) and value.get("error"):
            raise RuntimeError(f"Gmail MCP error: {value['error']}")
        result = value.get("result", value) if isinstance(value, dict) else value
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError(f"Gmail MCP tool error: {result.get('content', result)}")
        return result


class GmailAPIClient:
    """Direct Gmail and People REST fallback for MCP gaps."""

    def __init__(self, config: GmailConfig) -> None:
        self.config = config
        self.tokens = _TokenSource(config)

    def _headers(self, *, people: bool = False) -> dict[str, str]:
        env_name = self.config.direct_api_access_token_env
        token = self.tokens.get(env_name)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        people: bool = False,
    ) -> Any:
        base = self.config.people_api_base_url if people else self.config.api_base_url
        response = httpx.request(
            method,
            f"{base.rstrip('/')}/{path.lstrip('/')}",
            headers=self._headers(people=people),
            json=json_body,
            params=params,
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def send_message(self, arguments: dict[str, Any], workspace: Path) -> Any:
        message = EmailMessage()
        recipients = arguments.get("to", [])
        if not isinstance(recipients, list) or not recipients:
            raise ValueError("to must contain at least one email address")
        message["To"] = ", ".join(str(item) for item in recipients)
        for header in ("cc", "bcc"):
            values = arguments.get(header, [])
            if values:
                message[header.title()] = ", ".join(str(item) for item in values)
        message["Subject"] = str(arguments.get("subject", ""))
        if arguments.get("in_reply_to"):
            message["In-Reply-To"] = str(arguments["in_reply_to"])
            message["References"] = str(arguments["in_reply_to"])
        message.set_content(str(arguments.get("body", "")))
        for raw in arguments.get("attachments", []) or []:
            path = Path(str(raw)).expanduser()
            if not path.is_absolute():
                path = workspace / path
            path = path.resolve()
            if not path.is_file():
                raise ValueError(f"attachment does not exist: {path}")
            content_type, _ = mimetypes.guess_type(path.name)
            maintype, subtype = (content_type or "application/octet-stream").split("/", 1)
            message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
        body: dict[str, Any] = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}
        if arguments.get("thread_id"):
            body["threadId"] = str(arguments["thread_id"])
        return self.request("POST", "messages/send", json_body=body)

    def modify(self, message_id: str, add: list[str], remove: list[str]) -> Any:
        return self.request(
            "POST",
            f"messages/{message_id}/modify",
            json_body={"addLabelIds": add, "removeLabelIds": remove},
        )

    def download_attachment(self, message_id: str, attachment_id: str, path: Path) -> dict[str, Any]:
        result = self.request("GET", f"messages/{message_id}/attachments/{attachment_id}")
        data = result.get("data")
        if not isinstance(data, str):
            raise RuntimeError("Gmail attachment response did not contain data")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        return {"path": str(path), "bytes": path.stat().st_size}


class GmailToolService:
    def __init__(self, config: GmailConfig, confirmation: ConfirmationManager | None = None) -> None:
        self.config = config
        self.mcp = GmailMCPClient(config)
        self.api = GmailAPIClient(config)
        self.confirmation = confirmation

    def _confirm(self, tool: str, args: dict[str, Any], description: str, context: ToolContext) -> ToolResult | None:
        if self.confirmation is None:
            return None
        message = self.confirmation.require(tool, args, description, context.user_request)
        return ToolResult(message, is_error=True) if message else None

    def official(self, mcp_name: str, arguments: dict[str, Any]) -> str:
        return json.dumps(self.mcp.call(mcp_name, arguments), ensure_ascii=False, default=str)

    def _direct_allowed(self) -> None:
        if not self.config.direct_api_enabled:
            raise RuntimeError("direct Gmail API fallback is disabled in configuration")

    def _send_unconfirmed(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(json.dumps(self.api.send_message(args, context.workspace), default=str))

    def send(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        self._direct_allowed()
        blocked = self._confirm("gmail_send", args, f"send an email to {', '.join(map(str, args.get('to', [])))}", context)
        if blocked:
            return blocked
        return self._send_unconfirmed(args, context)

    def reply(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        self._direct_allowed()
        blocked = self._confirm("gmail_reply", args, f"reply to message {args.get('message_id', '')}", context)
        if blocked:
            return blocked
        return self._send_unconfirmed({**args, "thread_id": args.get("thread_id")}, context)

    def forward(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        self._direct_allowed()
        blocked = self._confirm("gmail_forward", args, f"forward message {args.get('message_id', '')}", context)
        if blocked:
            return blocked
        return self._send_unconfirmed(args, context)

    def destructive(self, action: str, args: dict[str, Any], context: ToolContext) -> ToolResult:
        self._direct_allowed()
        target = args.get("message_id") or args.get("thread_id") or args.get("label_id") or "the selected Gmail resource"
        blocked = self._confirm(f"gmail_{action}", args, f"{action} {target}", context)
        if blocked:
            return blocked
        if action == "trash":
            return ToolResult(json.dumps(self.api.modify(str(args["message_id"]), ["TRASH"], []), default=str))
        if action == "delete":
            return ToolResult(json.dumps(self.api.request("DELETE", f"messages/{args['message_id']}"), default=str))
        if action == "delete_label":
            return ToolResult(json.dumps(self.api.request("DELETE", f"labels/{args['label_id']}"), default=str))
        if action == "delete_filter":
            return ToolResult(json.dumps(self.api.request("DELETE", f"settings/filters/{args['filter_id']}"), default=str))
        raise ValueError(f"unsupported destructive Gmail action: {action}")

    def modify_labels(self, args: dict[str, Any], _context: ToolContext) -> ToolResult:
        self._direct_allowed()
        return ToolResult(json.dumps(self.api.modify(str(args["message_id"]), args.get("add_label_ids", []), args.get("remove_label_ids", [])), default=str))

    def create_label(self, args: dict[str, Any], _context: ToolContext) -> ToolResult:
        self._direct_allowed()
        return ToolResult(json.dumps(self.api.request("POST", "labels", json_body={
            "name": args["name"],
            "labelListVisibility": args.get("label_list_visibility", "labelShow"),
            "messageListVisibility": args.get("message_list_visibility", "show"),
        }), default=str))

    def filter(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        self._direct_allowed()
        action = str(args["action"])
        if action == "list":
            return ToolResult(json.dumps(self.api.request("GET", "settings/filters"), default=str))
        if action == "delete":
            return self.destructive("delete_filter", args, context)
        if action == "create":
            blocked = self._confirm("gmail_create_filter", args, "create a Gmail filter", context)
            if blocked:
                return blocked
            return ToolResult(json.dumps(self.api.request("POST", "settings/filters", json_body=args["criteria_action"]), default=str))
        raise ValueError("filter action must be list, create, or delete")

    def settings(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        self._direct_allowed()
        setting = str(args["setting"])
        paths = {
            "imap": "settings/imap",
            "pop": "settings/pop",
            "vacation": "settings/vacation",
            "send_as": "settings/sendAs",
        }
        path = paths[setting]
        if args["action"] == "get":
            return ToolResult(json.dumps(self.api.request("GET", path), default=str))
        blocked = self._confirm("gmail_update_settings", args, f"update Gmail {setting} settings", context)
        if blocked:
            return blocked
        return ToolResult(json.dumps(self.api.request("PUT", path, json_body=args.get("values", {})), default=str))

    def contacts(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        self._direct_allowed()
        action = str(args["action"])
        if action == "list":
            return ToolResult(json.dumps(self.api.request("GET", "people/me/connections", params={"personFields": "names,emailAddresses,phoneNumbers", "pageSize": args.get("page_size", 100)}, people=True), default=str))
        blocked = self._confirm(f"gmail_{action}_contact", args, f"{action} a Gmail contact", context)
        if blocked:
            return blocked
        if action == "create":
            return ToolResult(json.dumps(self.api.request("POST", "people:createContact", json_body=args["person"], people=True), default=str))
        resource = str(args["resource_name"])
        if action == "update":
            return ToolResult(json.dumps(self.api.request("PATCH", resource, params={"updatePersonFields": "names,emailAddresses,phoneNumbers"}, json_body=args["person"], people=True), default=str))
        if action == "delete":
            return ToolResult(json.dumps(self.api.request("DELETE", resource, people=True), default=str))
        raise ValueError("unsupported contact action")

    def attachment(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        self._direct_allowed()
        output = str(args["path"])
        path = Path(output).expanduser()
        if not path.is_absolute():
            path = context.workspace / path
        path = path.resolve()
        return ToolResult(json.dumps(self.api.download_attachment(str(args["message_id"]), str(args["attachment_id"]), path), default=str))


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def _register(registry: ToolRegistry, name: str, description: str, properties: dict[str, Any], handler: Any, required: list[str] | None = None) -> None:
    registry.register(Tool(name, description, _schema(properties, required), handler))


def register_gmail_tools(registry: ToolRegistry, config: GmailConfig, confirmation: ConfirmationManager | None = None) -> GmailToolService:
    service = GmailToolService(config, confirmation)
    string = {"type": "string"}
    strings = {"type": "array", "items": string}
    integer = {"type": "integer", "minimum": 1, "maximum": 1000}

    def official(name: str):
        def handler(args: dict[str, Any], _context: ToolContext) -> ToolResult:
            try:
                return ToolResult(service.official(name, args))
            except Exception as exc:
                log_error(f"gmail MCP {name} failed: {type(exc).__name__}: {exc}")
                return ToolResult(f"Gmail MCP {name} failed: {type(exc).__name__}: {exc}", is_error=True)
        return handler

    _register(registry, "gmail_search_threads", "Search Gmail threads using Gmail query syntax.", {"query": string, "pageSize": integer, "pageToken": string, "includeTrash": {"type": "boolean"}, "view": string}, official("search_threads"))
    _register(registry, "gmail_get_message", "Read one Gmail message by ID.", {"messageId": string, "messageFormat": string}, official("get_message"), ["messageId"])
    _register(registry, "gmail_get_thread", "Read a Gmail thread by ID.", {"threadId": string, "messageFormat": string}, official("get_thread"), ["threadId"])
    _register(registry, "gmail_create_draft", "Create a Gmail draft without sending it.", {"to": strings, "cc": strings, "bcc": strings, "subject": string, "body": string, "htmlBody": string, "replyToMessageId": string}, official("create_draft"))
    _register(registry, "gmail_list_drafts", "List Gmail drafts.", {"query": string, "pageSize": integer, "pageToken": string}, official("list_drafts"))
    _register(registry, "gmail_list_labels", "List Gmail labels.", {}, official("list_labels"))
    for exposed, remote, id_key in (("gmail_label_message", "label_message", "messageId"), ("gmail_label_thread", "label_thread", "threadId"), ("gmail_unlabel_message", "unlabel_message", "messageId"), ("gmail_unlabel_thread", "unlabel_thread", "threadId")):
        _register(registry, exposed, f"Gmail {remote.replace('_', ' ')}.", {id_key: string, "labelIds": strings}, official(remote), [id_key])

    mail_props = {"to": strings, "cc": strings, "bcc": strings, "subject": string, "body": string, "attachments": strings, "thread_id": string, "message_id": string, "in_reply_to": string}
    _register(registry, "gmail_send", "Send an email through the direct Gmail API fallback.", mail_props, service.send, ["to", "body"])
    _register(registry, "gmail_reply", "Reply to a Gmail message through the direct API fallback.", mail_props, service.reply, ["message_id", "to", "body"])
    _register(registry, "gmail_forward", "Forward a Gmail message through the direct API fallback.", mail_props, service.forward, ["message_id", "to", "body"])
    _register(registry, "gmail_trash", "Move one Gmail message to Trash.", {"message_id": string}, lambda a, c: service.destructive("trash", a, c), ["message_id"])
    _register(registry, "gmail_delete", "Permanently delete one Gmail message.", {"message_id": string}, lambda a, c: service.destructive("delete", a, c), ["message_id"])
    _register(registry, "gmail_modify_labels", "Add and remove labels on a Gmail message.", {"message_id": string, "add_label_ids": strings, "remove_label_ids": strings}, service.modify_labels, ["message_id"])
    _register(registry, "gmail_create_label", "Create a Gmail label.", {"name": string, "label_list_visibility": string, "message_list_visibility": string}, service.create_label, ["name"])
    _register(registry, "gmail_delete_label", "Delete a Gmail label.", {"label_id": string}, lambda a, c: service.destructive("delete_label", a, c), ["label_id"])
    _register(registry, "gmail_filters", "List, create, or delete Gmail filters.", {"action": {"type": "string", "enum": ["list", "create", "delete"]}, "filter_id": string, "criteria_action": {"type": "object"}}, service.filter, ["action"])
    _register(registry, "gmail_settings", "Read or update Gmail IMAP, POP, vacation, or send-as settings.", {"action": {"type": "string", "enum": ["get", "update"]}, "setting": {"type": "string", "enum": ["imap", "pop", "vacation", "send_as"]}, "values": {"type": "object"}}, service.settings, ["action", "setting"])
    _register(registry, "gmail_contacts", "List, create, update, or delete Google contacts.", {"action": {"type": "string", "enum": ["list", "create", "update", "delete"]}, "resource_name": string, "person": {"type": "object"}, "page_size": integer}, service.contacts, ["action"])
    _register(registry, "gmail_download_attachment", "Download a Gmail attachment into the workspace.", {"message_id": string, "attachment_id": string, "path": string}, service.attachment, ["message_id", "attachment_id", "path"])
    log_info(f"Gmail tools registered at {config.mcp_url}")
    return service
