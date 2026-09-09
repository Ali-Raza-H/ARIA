from dataclasses import replace
from pathlib import Path

import pytest

from aria.config import ConfigError, load_config, save_runtime_state


def write_config(tmp_path: Path, coder: str = "") -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "provider: ollama\n"
        "model: gemma2:9b\n"
        "workspace: .\n"
        "providers:\n"
        "  ollama:\n"
        "    host: http://127.0.0.1:11434\n"
        "    models: [gemma2:9b, qwen2.5-coder:7b]\n"
        f"{coder}",
        encoding="utf-8",
    )
    return path


def test_env_overrides_match_documented_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIA_CODER_MAX_ITERATIONS", "100")
    monkeypatch.setenv("ARIA_CODER_MAX_OUTPUT_CHARS", "20000")
    monkeypatch.setenv("ARIA_COMMAND_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("ARIA_MAX_COMMAND_OUTPUT_CHARS", "12000")
    monkeypatch.setenv("ARIA_SPEECH_ENABLED", "true")
    monkeypatch.setenv("ARIA_LOG_DIR", "custom-logs")

    config = load_config(write_config(tmp_path), tmp_path)

    assert config.coder.max_iterations == 100
    assert config.coder.max_output_chars == 20_000
    assert config.command_timeout_seconds == 30.0
    assert config.max_command_output_chars == 12_000
    assert config.speech.enabled is True
    assert config.logging.directory == Path("custom-logs")


def test_mistral_provider_and_kokoro_modes_are_loaded(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        "speech:\n  engine: kokoro_local\n"
        "providers:\n  mistral:\n    aria_api_key_env: ARIA_MISTRAL_API_KEY\n    coder_api_key_env: CODER_MISTRAL_API_KEY\n    base_url: https://api.mistral.ai/v1\n",
    )
    config = load_config(path, tmp_path)

    assert config.speech.engine == "kokoro_local"
    assert "mistral" in config.providers


def test_legacy_kokoro_engine_aliases_to_hosted_mode(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, "speech:\n  engine: kokoro\n"), tmp_path)

    assert config.speech.engine == "kokoro_hf"


def test_zai_provider_is_known(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        "providers:\n  zai:\n    aria_api_key_env: ARIA_ZAI_API_KEY\n"
        "    coder_api_key_env: CODER_ZAI_API_KEY\n"
        "    base_url: https://api.z.ai/api/paas/v4\n",
    )
    config = load_config(path, tmp_path)

    assert "zai" in config.providers


def test_memory_config_fields_match_example(tmp_path: Path) -> None:
    """The shipped example must parse cleanly and set every memory field."""
    repo_example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    config = load_config(repo_example, tmp_path)

    memory = config.memory
    assert memory.enabled is True
    assert memory.rank_similarity > 0 and memory.rank_importance > 0
    assert memory.rank_confidence >= 0
    assert memory.episodic_retention_days > 0
    assert memory.conversation_retention_days > 0
    assert memory.knowledge_retention_days >= 0
    assert memory.context_token_budget > 0
    assert "qwen3-embedding:0.6b" in memory.embedding_fallback_models


def test_coder_provider_and_model_are_loaded_from_config(tmp_path: Path) -> None:
    config = load_config(
        write_config(
            tmp_path,
            "coder:\n  provider: ollama\n  model: qwen2.5-coder:7b\n",
        ),
        tmp_path,
    )

    assert config.coder.provider == "ollama"
    assert config.coder.model == "qwen2.5-coder:7b"


def test_coder_backend_settings_are_optional_for_backward_compatibility(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path), tmp_path)

    assert config.coder.provider is None
    assert config.coder.model is None


def test_coder_provider_must_be_known(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="coder provider"):
        load_config(
            write_config(tmp_path, "coder:\n  provider: missing\n  model: test\n"),
            tmp_path,
        )


def test_runtime_state_restores_preferences_without_rewriting_config(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    initial = load_config(config_path, tmp_path)
    original_config = config_path.read_text(encoding="utf-8")
    state_path = tmp_path / "data" / "aria-state.yaml"
    save_runtime_state(state_path, initial, show_cot=False, coder_max_iterations=99)

    restored = load_config(config_path, tmp_path)

    assert restored.provider == "ollama"
    assert restored.model == "gemma2:9b"
    assert restored.max_iterations == initial.max_iterations
    assert restored.coder.max_iterations == 99
    assert restored.show_cot is False
    assert restored.runtime_state_path == state_path
    assert config_path.read_text(encoding="utf-8") == original_config


def test_runtime_state_overrides_static_provider_and_model(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    initial = load_config(config_path, tmp_path)
    changed = replace(initial, provider="mistral", model="mistral-small-latest")
    save_runtime_state(tmp_path / "data" / "aria-state.yaml", changed)

    restored = load_config(config_path, tmp_path)

    assert restored.provider == "mistral"
    assert restored.model == "mistral-small-latest"


def test_environment_override_beats_runtime_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_config(tmp_path)
    initial = load_config(config_path, tmp_path)
    changed = replace(initial, provider="mistral", model="mistral-small-latest")
    save_runtime_state(tmp_path / "data" / "aria-state.yaml", changed)
    monkeypatch.setenv("ARIA_PROVIDER", "ollama")
    monkeypatch.setenv("ARIA_MODEL", "gemma2:9b")

    restored = load_config(config_path, tmp_path)

    assert restored.provider == "ollama"
    assert restored.model == "gemma2:9b"
