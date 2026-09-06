import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from aria.config import ConfigError, load_config
from aria.tools import ToolContext, ToolRegistry, register_lifeos_tool


API_KEY_ENV = "ARIA_TEST_LIFEOS_API_KEY"
API_KEY = "test-key-123"


class FakeLifeOSHandler(BaseHTTPRequestHandler):
    """Minimal LifeOS API double: records requests, answers with JSON."""

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "user_agent": self.headers.get("User-Agent"),
                "idempotency_key": self.headers.get("Idempotency-Key"),
                "body": json.loads(raw_body) if raw_body else None,
            }
        )
        if self.path.startswith("/api/v1/assistant/tasks/7"):
            self._respond(200, {"task": {"id": 7, "title": "existing"}})
        elif self.path.startswith("/api/v1/assistant/tasks"):
            self._respond(200, {"tasks": [{"id": 1, "title": "first"}]})
        else:
            self._respond(200, {"ok": True})

    do_GET = _handle
    do_POST = _handle
    do_PATCH = _handle

    def log_message(self, format: str, *args) -> None:  # keep test output clean
        pass


@pytest.fixture()
def lifeos_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLifeOSHandler)
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def make_registry(base_url: str) -> ToolRegistry:
    from aria.config import LifeOSConfig

    registry = ToolRegistry()
    config = LifeOSConfig(
        base_url=base_url,
        api_key_env=API_KEY_ENV,
        timeout_seconds=5,
        max_retries=1,
        retry_backoff_seconds=0.01,
    )
    register_lifeos_tool(registry, config)
    return registry


@pytest.fixture()
def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolContext:
    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    return ToolContext(tmp_path)


def execute(registry: ToolRegistry, context: ToolContext, arguments: dict):
    result = registry.execute("lifeos", arguments, context)
    assert not result.is_error, result.output
    return json.loads(result.output)


# ------------------------------------------------------------------- happy path


def test_list_tasks_sends_auth_and_query(lifeos_server, context) -> None:
    registry = make_registry(f"http://127.0.0.1:{lifeos_server.server_address[1]}")
    result = execute(registry, context, {"operation": "list_tasks", "arguments": {"status": "open"}})

    assert result["success"] is True
    assert result["data"] == {"tasks": [{"id": 1, "title": "first"}]}
    request = lifeos_server.requests[-1]
    assert request["method"] == "GET"
    assert request["path"] == "/api/v1/assistant/tasks?status=open"
    assert request["authorization"] == f"Bearer {API_KEY}"
    assert request["user_agent"] == "ARIA-LifeOS/1.0"


def test_create_task_sends_json_body_and_idempotency_key(lifeos_server, context) -> None:
    registry = make_registry(f"http://127.0.0.1:{lifeos_server.server_address[1]}")
    result = execute(
        registry,
        context,
        {"operation": "create_task", "arguments": {"title": "write tests", "idempotency_key": "plan-42"}},
    )

    assert result["success"] is True
    request = lifeos_server.requests[-1]
    assert request["method"] == "POST"
    assert request["path"] == "/api/v1/assistant/tasks"
    assert request["body"] == {"title": "write tests"}
    assert request["idempotency_key"] == "plan-42"


def test_create_task_generates_idempotency_key(lifeos_server, context) -> None:
    registry = make_registry(f"http://127.0.0.1:{lifeos_server.server_address[1]}")
    execute(registry, context, {"operation": "create_task", "arguments": {"title": "x"}})
    request = lifeos_server.requests[-1]
    assert request["idempotency_key"]  # auto-generated, non-empty


def test_complete_task_sets_status(lifeos_server, context) -> None:
    registry = make_registry(f"http://127.0.0.1:{lifeos_server.server_address[1]}")
    execute(registry, context, {"operation": "complete_task", "arguments": {"task_id": 7}})

    request = lifeos_server.requests[-1]
    assert request["method"] == "PATCH"
    assert request["path"] == "/api/v1/assistant/tasks/7"
    assert request["body"] == {"status": "completed"}


def test_path_argument_is_substituted(lifeos_server, context) -> None:
    registry = make_registry(f"http://127.0.0.1:{lifeos_server.server_address[1]}")
    result = execute(registry, context, {"operation": "get_task", "arguments": {"task_id": 7}})
    assert result["data"]["task"]["id"] == 7


# ------------------------------------------------------------------- errors


def test_unknown_operation_lists_available(lifeos_server, context) -> None:
    registry = make_registry(f"http://127.0.0.1:{lifeos_server.server_address[1]}")
    result = registry.execute("lifeos", {"operation": "teleport"}, context)
    assert result.is_error
    assert "Unknown LifeOS operation: teleport" in result.output
    assert "create_task" in result.output


def test_invalid_path_argument_is_reported(lifeos_server, context) -> None:
    registry = make_registry(f"http://127.0.0.1:{lifeos_server.server_address[1]}")
    result = registry.execute("lifeos", {"operation": "get_task", "arguments": {"task_id": 0}}, context)
    assert result.is_error
    assert "task_id must be a positive integer" in result.output


def test_unconfigured_base_url_disables_tool(tmp_path: Path) -> None:
    from aria.config import LifeOSConfig

    registry = ToolRegistry()
    register_lifeos_tool(registry, LifeOSConfig(base_url=""))
    result = registry.execute("lifeos", {"operation": "list_tasks"}, ToolContext(tmp_path))
    assert result.is_error
    assert "not configured" in result.output


def test_disabled_config_disables_tool(tmp_path: Path) -> None:
    from aria.config import LifeOSConfig

    registry = ToolRegistry()
    register_lifeos_tool(registry, LifeOSConfig(enabled=False, base_url="http://127.0.0.1:1"))
    result = registry.execute("lifeos", {"operation": "list_tasks"}, ToolContext(tmp_path))
    assert result.is_error
    assert "disabled" in result.output


def test_missing_api_key_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from aria.config import LifeOSConfig

    monkeypatch.delenv(API_KEY_ENV, raising=False)
    registry = ToolRegistry()
    register_lifeos_tool(registry, LifeOSConfig(base_url="http://127.0.0.1:1", api_key_env=API_KEY_ENV))
    result = registry.execute("lifeos", {"operation": "list_tasks"}, ToolContext(tmp_path))
    assert result.is_error
    assert "not configured" in result.output


def test_http_error_surfaces_status_and_message(lifeos_server, context, monkeypatch: pytest.MonkeyPatch) -> None:
    def not_found(self) -> None:
        self._respond(404, {"message": "task vanished"})

    monkeypatch.setattr(FakeLifeOSHandler, "do_GET", not_found)
    registry = make_registry(f"http://127.0.0.1:{lifeos_server.server_address[1]}")
    result = registry.execute("lifeos", {"operation": "list_tasks"}, context)
    assert result.is_error
    payload = json.loads(result.output)
    assert payload["statusCode"] == 404
    assert payload["error"] == "task vanished"


def test_retries_on_503_then_succeeds(lifeos_server, context, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}
    lock = threading.Lock()

    def flaky(self) -> None:
        with lock:
            attempts["count"] += 1
            first = attempts["count"] == 1
        self._respond(503 if first else 200, {"ok": not first})

    monkeypatch.setattr(FakeLifeOSHandler, "do_GET", flaky)
    registry = make_registry(f"http://127.0.0.1:{lifeos_server.server_address[1]}")
    result = execute(registry, context, {"operation": "list_tasks"})
    assert result["success"] is True
    assert attempts["count"] == 2


def test_connection_error_reports_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from aria.config import LifeOSConfig

    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    registry = ToolRegistry()
    register_lifeos_tool(
        registry,
        LifeOSConfig(base_url="http://127.0.0.1:1", api_key_env=API_KEY_ENV, max_retries=0),
    )
    result = registry.execute("lifeos", {"operation": "list_tasks"}, ToolContext(tmp_path))
    assert result.is_error
    assert "LifeOS request failed" in result.output


# ------------------------------------------------------------------- config


def test_lifeos_config_defaults_from_yaml(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("provider: ollama\nmodel: test\nworkspace: .\n", encoding="utf-8")
    config = load_config(config_file, tmp_path)
    assert config.lifeos.base_url == ""
    assert config.lifeos.api_key_env == "LIFEOS_API_KEY"
    assert config.lifeos.timeout_seconds == 15.0


def test_lifeos_config_parses_yaml_section(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "provider: ollama\nmodel: test\nworkspace: .\n"
        "lifeos:\n  base_url: http://127.0.0.1:5000/\n  timeout_seconds: 3\n  max_retries: 2\n",
        encoding="utf-8",
    )
    config = load_config(config_file, tmp_path)
    assert config.lifeos.base_url == "http://127.0.0.1:5000"  # trailing slash stripped
    assert config.lifeos.timeout_seconds == 3.0
    assert config.lifeos.max_retries == 2


def test_lifeos_config_rejects_bad_values(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "provider: ollama\nmodel: test\nworkspace: .\n"
        "lifeos:\n  timeout_seconds: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_file, tmp_path)
