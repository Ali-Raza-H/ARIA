from pathlib import Path

from aria.config import BackgroundConfig, LifeOSConfig, SchedulerConfig
from aria.services.proactive import ProactiveService
from aria.services.scheduler import ScheduledJob, SchedulerService
from aria.telemetry import TelemetryConfig, TelemetryRecorder


def test_briefing_returns_agent_input_instead_of_json_notification() -> None:
    service = ProactiveService(
        LifeOSConfig(enabled=False),
        BackgroundConfig(enabled=False),
        SchedulerConfig(enabled=False),
        Path("/tmp/aria-test-profile.json"),
    )
    service.collect_context = lambda: {"list_tasks": {"items": []}}  # type: ignore[method-assign]
    result = service.briefing("morning")
    assert "agent_input" in result
    assert "SOURCE DATA:" in result["agent_input"]
    assert "notification" not in result


def test_scheduler_narrates_briefing_before_delivery(tmp_path: Path) -> None:
    delivered: list[str] = []
    narrated: list[str] = []

    def action_runner(_category: str, _action: dict) -> dict:
        return {"agent_input": "A scheduled morning briefing is due. SOURCE DATA: {}"}

    service = SchedulerService(
        SchedulerConfig(
            enabled=True,
            database=tmp_path / "scheduler.sqlite3",
            workflow_directory=tmp_path / "workflows",
            default_workflows=False,
        ),
        # Analysis is allowed independently of write autonomy.
        __import__("aria.config", fromlist=["AutonomyConfig"]).AutonomyConfig(enabled=False),
        action_runner,
        lambda title, body, _urgency: delivered.append(f"{title}: {body}"),
        narrate=lambda prompt: narrated.append(prompt) or "Good morning. Here is your briefing.",
    )
    try:
        service.store.upsert_job(ScheduledJob("briefing", "briefing", "0 8 * * *", "analysis", {"type": "briefing"}))
        service.tick(__import__("datetime").datetime(2026, 9, 10, 8, 0))
    finally:
        service.stop()
    assert narrated == ["A scheduled morning briefing is due. SOURCE DATA: {}"]
    assert delivered == ["ARIA · briefing: Good morning. Here is your briefing."]


def test_context_window_is_stored_and_reported(tmp_path: Path) -> None:
    recorder = TelemetryRecorder(TelemetryConfig(database=tmp_path / "t.sqlite3"), tmp_path)
    recorder.register_model_contexts(
        {"ollama": {"models": ["model", "unused"], "context_windows": {"model": 32768, "unused": 8192}}}
    )
    turn = recorder.start_turn("hello")
    call, started, _ = recorder.start_model_call(
        turn,
        provider="ollama",
        model="model",
        role="aria",
        iteration=1,
        messages=[{"role": "user", "content": "hello"}],
        tool_count=0,
        context_window=32768,
    )
    recorder.finish_model_call(call, started, response="ok", usage={"prompt_eval_count": 5, "eval_count": 2})
    snapshot = recorder.snapshot()
    recorder.close()
    assert snapshot["context_window"] == 32768
    assert snapshot["context_tokens"] > 0
    assert {item["model"] for item in snapshot["model_contexts"]} == {"model", "unused"}
    assert snapshot["model_contexts"][0]["context_window"] in {8192, 32768}
