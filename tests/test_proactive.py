import json
from pathlib import Path

from aria.config import (
    AutonomyConfig,
    BackgroundConfig,
    LifeOSConfig,
    SchedulerConfig,
    VisionConfig,
)
from aria.images import ImageAttachment, prepare_image_message, register_image_tools
from aria.llm.base import AssistantMessage
from aria.llm.ollama_provider import OllamaProvider
from aria.proactive import ProactiveService
from aria.scheduler import CronSchedule, SchedulerService, SchedulerStore
from aria.tools.base import ToolContext
from aria.tools.registry import ToolRegistry


class TextProvider:
    supports_image_input = False

    def complete(self, messages, tools, on_text=None):
        return AssistantMessage("analysis")


def test_cron_matches_system_local_fields() -> None:
    schedule = CronSchedule.parse("*/15 8-9 * * 1-5")

    from datetime import datetime

    assert schedule.matches(datetime(2026, 9, 8, 8, 15))
    assert not schedule.matches(datetime(2026, 9, 8, 10, 15))


def test_session_timers_are_removed_on_store_reopen(tmp_path: Path) -> None:
    database = tmp_path / "scheduler.sqlite3"
    first = SchedulerStore(database)
    first.add_timer("session", "timer", 60, False)
    first.add_timer("persistent", "timer", 60, True)
    first.close()

    second = SchedulerStore(database)
    assert [row["name"] for row in second.timers()] == ["persistent"]
    second.close()


def test_autonomous_lifeos_write_requires_operation_allowlist(tmp_path: Path) -> None:
    service = ProactiveService(
        LifeOSConfig(enabled=False),
        BackgroundConfig(enabled=False),
        SchedulerConfig(),
        tmp_path / "profile.json",
    )

    try:
        service.run_action(
            "lifeos_writes",
            {"type": "write", "operation": "create_task", "arguments": {}},
            AutonomyConfig(enabled=True, allowed_categories=("lifeos_writes",), lifeos_write_operations=()),
        )
    except PermissionError as exc:
        assert "not allowlisted" in str(exc)
    else:
        raise AssertionError("unallowlisted LifeOS write was accepted")


def test_calendar_conflicts_are_derived_deterministically(tmp_path: Path) -> None:
    service = ProactiveService(
        LifeOSConfig(enabled=False),
        BackgroundConfig(enabled=False),
        SchedulerConfig(),
        tmp_path / "profile.json",
    )
    context = {
        "list_calendar": {
            "events": [
                {"title": "A", "start": "2026-09-08T10:00:00+00:00", "end": "2026-09-08T11:00:00+00:00"},
                {"title": "B", "start": "2026-09-08T10:30:00+00:00", "end": "2026-09-08T11:30:00+00:00"},
            ]
        }
    }

    signals = service._derive_signals(context)

    assert signals["calendar_events_checked"] == 2
    assert signals["calendar_conflicts"] == [{"first": "A", "second": "B", "starts": "2026-09-08T10:30:00+00:00"}]


def test_disabled_vision_does_not_register_screen_tool() -> None:
    registry = ToolRegistry()
    register_image_tools(registry, TextProvider(), VisionConfig(enabled=False))

    assert "screen_capture" not in registry.names()


def test_ollama_normalizes_openai_style_image_parts() -> None:
    image = ImageAttachment(b"png", "image/png", "test")
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": image.data_url}},
        ],
    }

    normalized = OllamaProvider._normalize_message(message)

    assert normalized["content"] == "describe"
    assert normalized["images"] == ["cG5n"]


def test_scheduler_audits_blocked_category(tmp_path: Path) -> None:
    calls: list[dict] = []
    notifications: list[str] = []
    config = SchedulerConfig(
        enabled=True,
        database=tmp_path / "scheduler.sqlite3",
        workflow_directory=tmp_path / "workflows",
        default_workflows=False,
    )
    service = SchedulerService(
        config,
        AutonomyConfig(enabled=True, allowed_categories=()),
        lambda category, action: calls.append(action) or {"notification": "should not run"},
        lambda title, body, urgency: notifications.append(body),
    )
    service.store.upsert_job(
        __import__("aria.scheduler", fromlist=["ScheduledJob"]).ScheduledJob(
            "blocked", "blocked", "* * * * *", "notifications", {"type": "notice", "body": "x"}
        )
    )

    from datetime import datetime

    service.tick(datetime(2026, 9, 8, 10, 15))

    assert calls == []
    assert notifications == []
    assert service.store.audit_entries(1)[0]["status"] == "blocked"
    service.stop()
