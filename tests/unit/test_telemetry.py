import sqlite3
from pathlib import Path

from aria.telemetry import TelemetryConfig, TelemetryRecorder


def test_token_estimate_is_deterministic() -> None:
    assert TelemetryRecorder.estimate_tokens("12345678") == 2
    assert TelemetryRecorder.estimate_tokens({"content": "12345678"}) >= 2


def test_telemetry_records_usage_without_content(tmp_path: Path) -> None:
    recorder = TelemetryRecorder(TelemetryConfig(database=tmp_path / "telemetry.sqlite3"), tmp_path)
    turn = recorder.start_turn("private prompt")
    call, started, _ = recorder.start_model_call(
        turn,
        provider="test",
        model="model-a",
        role="aria",
        iteration=1,
        messages=[{"role": "user", "content": "private prompt"}],
        tool_count=2,
    )
    class Response:
        content = "private response"
    recorder.finish_model_call(call, started, response=Response(), usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
    recorder.record_tool(turn, name="private_tool", arguments={"secret": "private"}, result="private result", ok=True, started=started)
    recorder.finish_turn(turn, response="private response", iterations=1, tool_calls=1)
    snapshot = recorder.snapshot()
    recorder.close()

    assert snapshot["input_tokens"] == 11
    assert snapshot["output_tokens"] == 7
    connection = sqlite3.connect(tmp_path / "telemetry.sqlite3")
    assert connection.execute("SELECT COUNT(*) FROM turns WHERE user_chars = 14").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM events WHERE payload_json LIKE '%private prompt%'").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM tool_calls WHERE argument_chars > 0 AND result_chars > 0").fetchone()[0] == 1
    connection.close()


def test_disabled_telemetry_has_no_database(tmp_path: Path) -> None:
    recorder = TelemetryRecorder(TelemetryConfig(enabled=False, database=tmp_path / "off.sqlite3"), tmp_path)
    turn = recorder.start_turn("x")
    recorder.finish_turn(turn, response="y", iterations=1, tool_calls=0)
    assert recorder.snapshot() == {"enabled": False}
    assert not (tmp_path / "off.sqlite3").exists()
