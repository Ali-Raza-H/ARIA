"""Regression tests for the startup/reliability bug report (BR-1 to BR-4)."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from aria.config import (
    RUNTIME_STATE_RELATIVE_PATH,
    AppConfig,
    ConfigError,
    load_config,
    save_runtime_state,
)
from aria.memory_pkg.config import MemorySettings
from aria.memory_pkg.embeddings import FallbackEmbeddingProvider
from aria.memory_pkg.exceptions import MemoryError
from aria.memory_pkg.manager import MemoryManager

# ────────────────────────────────────────────────────────────── shared fakes


class DummyAgent:
    model_name = "test-model"

    class memory:  # noqa: N801 - attribute container
        messages: list = []


def make_config(tmp_path: Path, **overrides: Any) -> AppConfig:
    config = AppConfig(
        provider="ollama",
        model="gemma2:9b",
        providers={},
        workspace=tmp_path,
        max_iterations=5,
        command_timeout_seconds=None,
        max_command_output_chars=None,
        persona="jarvis",
        runtime_state_path=tmp_path / "aria-state.yaml",
    )
    return replace(config, **overrides) if overrides else config


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "provider: ollama\n"
        "model: gemma2:9b\n"
        "workspace: .\n"
        "providers:\n"
        "  ollama:\n"
        "    host: http://127.0.0.1:11434\n"
        "    models: [gemma2:9b, qwen2.5-coder:7b]\n",
        encoding="utf-8",
    )
    return path


# ───────────────────────────────────────────────── BR-2: runtime state safety


def test_runtime_state_with_stale_workspace_and_model_is_sanitized(tmp_path: Path) -> None:
    """A stale/scratch value in state must drop one key, not break startup."""
    config_path = write_config(tmp_path)
    state_path = tmp_path / "data" / "aria-state.yaml"
    state_path.parent.mkdir()
    state_path.write_text(
        "provider: ollama\n"
        'model: ""\n'  # empty → dropped
        "workspace: /tmp/pytest-of-someone/pytest-1/deleted0\n"  # missing dir → dropped
        "max_iterations: 0\n"  # invalid → dropped
        "ui:\n"
        "  show_cot: false\n",
        encoding="utf-8",
    )

    config = load_config(config_path, tmp_path)

    assert config.model == "gemma2:9b"  # from config.yaml
    assert config.max_iterations == 20  # config.yaml default path
    assert config.show_cot is False  # valid ui section still applies
    assert set(config.runtime_state_overrides) == {"provider", "ui"}


def test_ignore_runtime_state_skips_saved_overrides(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    initial = load_config(config_path, tmp_path)
    save_runtime_state(
        tmp_path / "data" / "aria-state.yaml",
        replace(initial, provider="mistral", model="mistral-small-latest"),
    )

    restored = load_config(config_path, tmp_path, ignore_runtime_state=True)

    assert restored.provider == "ollama"
    assert restored.model == "gemma2:9b"
    assert restored.runtime_state_overrides == ()


def test_invalid_state_file_is_ignored_not_fatal(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    state_path = tmp_path / "data" / "aria-state.yaml"
    state_path.parent.mkdir()
    state_path.write_text("{ not: valid: yaml:", encoding="utf-8")

    config = load_config(config_path, tmp_path)

    assert config.model == "gemma2:9b"
    assert config.runtime_state_overrides == ()


def test_cli_flags_parse() -> None:
    from aria.__main__ import build_parser

    args = build_parser().parse_args(["--ignore-state"])
    assert args.ignore_state is True and args.reset_state is False
    args = build_parser().parse_args(["--reset-state"])
    assert args.reset_state is True and args.ignore_state is False
    args = build_parser().parse_args([])
    assert args.ignore_state is False and args.reset_state is False


class FakeModelManager:
    """ProviderManager double: static list + optional live inventory."""

    def __init__(self, static: list[str], live: list[str] | None) -> None:
        self._static = static
        self._live = live

    def models_for(self, provider: str) -> list[str]:
        return list(self._static)

    def create(self, provider: str, model: str) -> "FakeModelManager":
        if self._live is None:
            raise ValueError("server down")
        return self

    def list_models(self) -> list[str]:
        return list(self._live or [])


def test_model_validation_accepts_configured_model(tmp_path: Path) -> None:
    from aria.__main__ import _validate_model_choice

    config = make_config(tmp_path, model="gemma2:9b")
    manager = FakeModelManager(["gemma2:9b", "qwen2.5-coder:7b"], ["gemma2:9b"])

    assert _validate_model_choice(manager, config) is None


def test_model_validation_falls_back_when_state_model_is_missing(tmp_path: Path) -> None:
    from aria.__main__ import _validate_model_choice

    config = make_config(tmp_path, model="test", runtime_state_overrides=("model",))
    manager = FakeModelManager(["gemma2:9b", "qwen2.5-coder:7b"], ["qwen2.5-coder:7b"])

    fallback = _validate_model_choice(manager, config)

    assert fallback == "qwen2.5-coder:7b"  # first static model present in live list


def test_model_validation_continues_when_inventory_unavailable(tmp_path: Path) -> None:
    from aria.__main__ import _validate_model_choice

    config = make_config(tmp_path, model="test", runtime_state_overrides=("model",))
    manager = FakeModelManager(["gemma2:9b"], None)  # server down

    assert _validate_model_choice(manager, config) is None


# ─────────────────────────────────────── BR-1: SearXNG must not block startup


def test_web_start_backend_defaults_to_false(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path), tmp_path)
    assert config.web.start_backend is False


def test_web_start_backend_is_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "provider: ollama\n"
        "model: gemma2:9b\n"
        "web:\n"
        "  enabled: true\n"
        "  start_backend: true\n",
        encoding="utf-8",
    )

    assert load_config(path, tmp_path).web.start_backend is True


def test_unreachable_searxng_is_non_fatal_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BR-1: with start_backend=false, startup must not touch Docker at all."""
    from aria import __main__ as main_module

    monkeypatch.setattr(main_module, "_is_searxng_reachable", lambda url: False)
    monkeypatch.setattr(
        shutil, "which", lambda *a, **k: pytest.fail("docker must not be probed")
    )

    reached = main_module._ensure_web_backend(tmp_path, make_config(tmp_path).web)

    assert reached is False


# ───────────────────────────────────────── BR-3: urwid crashes are propagated


def test_urwid_run_reraises_ui_thread_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urwid

    from aria.ui.urwid_tui import UrwidRepl

    repl = UrwidRepl(cast(Any, DummyAgent()), config=make_config(tmp_path))

    def boom(self: urwid.MainLoop) -> None:
        raise RuntimeError("tui exploded")

    monkeypatch.setattr(urwid.MainLoop, "run", boom)

    with pytest.raises(RuntimeError, match="tui exploded"):
        repl.run()


def test_ui_factory_does_not_import_urwid_at_module_load() -> None:
    import sys

    import aria.ui.factory as factory

    assert hasattr(factory, "create_repl")
    # The lazy import keeps `import aria.ui.factory` working without the extra.
    assert "urwid" not in factory.__dict__


# ──────────────────────────────────── BR-4: degraded memory without embeddings


def _settings(tmp_path: Path) -> MemorySettings:
    return MemorySettings(
        root=tmp_path,
        sqlite_path=tmp_path / "assistant.db",
        chroma_path=tmp_path / "chroma",
        embedding_fallback_models=(),
    )


def test_fallback_embeddings_report_degraded_and_recover(tmp_path: Path) -> None:
    provider = FallbackEmbeddingProvider(_settings(tmp_path))

    with pytest.raises(MemoryError):
        provider.embed(["hello"])
    assert provider.degraded is True
    assert provider.backend_name == "none"

    # While degraded, retries short-circuit until the interval expires.
    with pytest.raises(MemoryError, match="retrying"):
        provider.embed(["hello again"])

    # A backend coming back recovers the provider.
    class Working:
        name = "working"

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] for _ in texts]

    provider._active = Working()
    assert provider.embed(["hello"]) == [[1.0]]
    assert provider.degraded is False


def test_probe_returns_false_when_no_backend_and_true_after_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FallbackEmbeddingProvider(_settings(tmp_path))
    assert provider.probe() is False
    assert provider.degraded is True

    class Working:
        name = "working"

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.5] for _ in texts]

    monkeypatch.setattr(provider, "_candidates", lambda: iter([Working()]))
    assert provider.probe() is True
    assert provider.degraded is False


def test_memory_manager_degraded_flag_reflects_embeddings(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path, _settings(tmp_path))
    try:
        # Plain providers without a degraded flag read as healthy.
        assert manager.degraded is False
    finally:
        manager.cleanup()


class DegradedEmbeddings:
    name = "degraded"

    @property
    def degraded(self) -> bool:
        return True

    def probe(self) -> bool:
        return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise MemoryError("no embedding backend")


def test_memory_manager_skips_semantic_writes_in_degraded_mode(tmp_path: Path) -> None:
    manager = MemoryManager(tmp_path, _settings(tmp_path), embeddings=DegradedEmbeddings())
    try:
        assert manager.degraded is True
        # Explicit semantic remember degrades to "not stored" without raising.
        assert manager.remember("User likes dark mode", "episodic") is None
        # History processing skips quietly too.
        manager.add({"role": "user", "content": "Remember that I use Neovim"})
        assert manager.summarize_now() == 1  # drained without raising
        assert manager.stats()["registry_active"] == 0
        # Structured (SQLite-only) writes keep working in degraded mode.
        assert manager.remember("pref", "preference", key="theme", value="dark")
        assert manager.facts_text() != ""
    finally:
        manager.cleanup()


def test_config_example_documents_degraded_memory_mode() -> None:
    repo_example = Path(__file__).resolve().parents[1] / "config.yaml"
    parsed = yaml.safe_load(repo_example.read_text(encoding="utf-8"))
    assert parsed["web"]["start_backend"] is False


def test_reset_state_flag_removes_state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--reset-state deletes the state file so config.yaml wins again."""
    from aria.__main__ import build_parser

    config_path = write_config(tmp_path)
    initial = load_config(config_path, tmp_path)
    state_path = tmp_path / RUNTIME_STATE_RELATIVE_PATH
    save_runtime_state(state_path, replace(initial, model="mistral-small-latest"))
    monkeypatch.chdir(tmp_path)

    args = build_parser().parse_args(["--reset-state"])
    state_file = tmp_path / RUNTIME_STATE_RELATIVE_PATH
    if args.reset_state:
        state_file.unlink(missing_ok=True)

    assert not state_file.exists()
    restored = load_config(config_path, tmp_path)
    assert restored.model == "gemma2:9b"
