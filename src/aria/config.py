"""Runtime configuration loading (YAML + .env overrides)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .logging_setup import log_debug, log_error, log_info


class ConfigError(ValueError):
    """Raised when the assistant configuration is missing or invalid."""


KNOWN_PROVIDERS = {
    "cerebras",
    "gemini",
    "groq",
    "nvidia",
    "openrouter",
    "openai",
    "mistral",
    "ollama",
}


@dataclass(frozen=True)
class SpeechConfig:
    """Text-to-speech settings.

    ``kokoro`` runs on Hugging Face-hosted inference (no local GPU needed, so
    it can share the machine with Ollama); ``chatterbox`` runs locally.
    """

    enabled: bool = False
    engine: str = "kokoro_hf"  # "kokoro_hf" | "kokoro_local" | "chatterbox"
    voice: str = "af_sarah"
    speed: float = 1.0
    lang_code: str = "a"
    hf_api_key_env: str = "HF_TOKEN"
    hf_model: str = "hexgrad/Kokoro-82M"
    hf_provider: str = "auto"


@dataclass(frozen=True)
class LoggingConfig:
    """Rotating file logging settings."""

    directory: Path = Path("data/logs")
    console_level: str = "WARNING"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 3


@dataclass(frozen=True)
class CoderConfig:
    """Settings for the independent coding sub-agent."""

    # When omitted, the coder uses ARIA's configured provider/model.
    provider: str | None = None
    model: str | None = None
    max_iterations: int = 60
    max_output_chars: int = 12_000


@dataclass(frozen=True)
class WebConfig:
    """Local SearXNG and bounded webpage research settings."""

    enabled: bool = True
    searxng_url: str = "http://127.0.0.1:8080"
    search_timeout: float = 10.0
    page_timeout: float = 15.0
    max_results: int = 5
    max_search_calls: int = 4
    max_page_fetches: int = 8
    max_total_web_calls: int = 12
    max_page_chars: int = 30_000
    max_snippet_chars: int = 2_000
    max_concurrent_page_fetches: int = 3
    max_retries: int = 2
    search_cache_ttl: int = 300
    page_cache_ttl: int = 1_800
    allowed_hosts: tuple[str, ...] = ()
    user_agent: str = "ARIA/1.0"


@dataclass(frozen=True)
class LifeOSConfig:
    """Connection to LifeOS, the personal-life API (tasks, projects, goals...).

    Disabled unless ``base_url`` and ``api_key`` are both set; the key itself
    lives in .env (``api_key_env`` names the variable that holds it).
    """

    enabled: bool = True  # only effective once base_url + api_key resolve
    base_url: str = ""
    api_key_env: str = "LIFEOS_API_KEY"
    timeout_seconds: float = 15.0
    max_retries: int = 1
    retry_backoff_seconds: float = 0.4


@dataclass(frozen=True)
class MemoryConfig:
    """Persistent three-tier memory settings."""

    enabled: bool = True
    directory: Path = Path("data/memory")
    chroma_directory: Path = Path("data/memory/chroma")
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_api_key_env: str = "MEMORY_EMBEDDING_API_KEY"
    embedding_ollama_host: str = "http://127.0.0.1:11434"
    embedding_fallback_models: tuple[str, ...] = (
        "qwen3-embedding:0.6b",
        "nomic-embed-text",
        "mxbai-embed-large",
        "bge-m3",
        "snowflake-arctic-embed",
    )
    raw_retention_days: int = 30
    archive_retention_days: int = 365
    tier2_retention_days: int = 730
    detail_mode: str = "raw_then_summary"
    context_token_budget: int = 4000
    tier2_limit: int = 8
    tier3_limit: int = 8
    rank_similarity: float = 0.25
    rank_recency: float = 0.15
    rank_frequency: float = 0.10
    rank_importance: float = 0.35
    rank_explicit: float = 0.15
    promotion_confidence: float = 0.80
    promotion_importance: float = 0.70
    promotion_repetitions: int = 2
    promotion_recency_days: int = 365
    retry_interval_seconds: int = 60
    warmup_turns: int = 1


@dataclass(frozen=True)
class AppConfig:
    """Validated runtime settings."""

    provider: str
    model: str
    providers: dict[str, dict[str, Any]]
    workspace: Path
    max_iterations: int
    command_timeout_seconds: float | None
    max_command_output_chars: int | None
    persona: str
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    coder: CoderConfig = field(default_factory=CoderConfig)
    lifeos: LifeOSConfig = field(default_factory=LifeOSConfig)
    web: WebConfig = field(default_factory=WebConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    show_cot: bool = True
    runtime_state_path: Path = Path("data/aria-state.yaml")


RUNTIME_STATE_RELATIVE_PATH = Path("data/aria-state.yaml")


def _load_runtime_state(path: Path) -> dict[str, Any]:
    """Load the small, non-secret file containing preferences changed in the REPL."""
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        log_error(f"Config: ignoring invalid runtime state {path}: {exc}")
        return {}
    if not isinstance(loaded, dict):
        log_error(f"Config: ignoring runtime state that is not a YAML object: {path}")
        return {}

    # Whitelist fields so this local state file cannot unexpectedly change
    # provider credentials, tool limits, or other static configuration.
    state: dict[str, Any] = {}
    for key in ("provider", "model", "workspace", "max_iterations"):
        if key in loaded:
            state[key] = loaded[key]
    for section in ("speech", "coder", "ui"):
        value = loaded.get(section)
        if isinstance(value, dict):
            state[section] = dict(value)
    return state


def _apply_runtime_state(raw: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Apply saved runtime preferences while retaining static YAML settings."""
    for key in ("provider", "model", "workspace", "max_iterations"):
        if key in state:
            raw[key] = state[key]
    for section in ("speech", "coder", "ui"):
        values = state.get(section)
        if not isinstance(values, dict):
            continue
        current = raw.get(section)
        if not isinstance(current, dict):
            current = {}
        current.update(values)
        raw[section] = current
    return raw


def save_runtime_state(
    path: Path,
    config: AppConfig,
    *,
    show_cot: bool | None = None,
    coder_max_iterations: int | None = None,
) -> None:
    """Atomically save non-secret preferences changed during this ARIA run."""
    state = {
        "version": 1,
        "provider": config.provider,
        "model": config.model,
        "workspace": str(config.workspace),
        "max_iterations": config.max_iterations,
        "coder": {
            "max_iterations": (
                coder_max_iterations
                if coder_max_iterations is not None
                else config.coder.max_iterations
            ),
        },
        "speech": {
            "enabled": config.speech.enabled,
            "engine": config.speech.engine,
        },
        "ui": {"show_cot": config.show_cot if show_cot is None else show_cot},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(yaml.safe_dump(state, sort_keys=False), encoding="utf-8")
    temporary.replace(path)


def _parse_env_value(value: str) -> Any:
    """Convert common scalar environment values without losing strings."""
    stripped = value.strip()
    lowered = stripped.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        return float(stripped) if any(character in stripped for character in ".eE") else int(stripped)
    except ValueError:
        return value


def _env_path(key: str) -> tuple[str, str] | None:
    """Map documented ARIA_* names to a YAML section and field.

    Underscores are valid inside field names, so blindly splitting an env name
    into one dictionary level per underscore would turn e.g.
    ``ARIA_CODER_MAX_ITERATIONS`` into the wrong shape.
    """
    suffix = key.removeprefix("ARIA_")
    if suffix == "LOG_DIR":
        return "logging", "directory"
    sections = {"CODER", "SPEECH", "LIFEOS", "LOGGING", "MEMORY"}
    first, separator, remainder = suffix.partition("_")
    if separator and first in sections and remainder:
        return first.lower(), remainder.lower()
    if suffix:
        return "", suffix.lower()
    return None


def _env_override(raw: dict[str, Any]) -> dict[str, Any]:
    """Apply ARIA_* environment variable overrides on top of the YAML values."""
    prefix = "ARIA_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        path = _env_path(key)
        if path is None:
            continue
        section, field_name = path
        if section:
            target = raw.get(section)
            if not isinstance(target, dict):
                target = {}
                raw[section] = target
        else:
            target = raw
        target[field_name] = _parse_env_value(value)
        log_debug(f"Config: env override {key} applied")
    return raw


def _optional_positive_number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{name} must be a positive number or null")
    return float(value)


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer or null")
    return value


def load_config(path: Path, launch_directory: Path | None = None) -> AppConfig:
    """Load config from YAML (+.env), resolving the workspace from the launch directory."""
    # Load .env from the config file's directory, then the launch directory.
    for base in (path.parent, launch_directory or Path.cwd()):
        env_file = base / ".env"
        if env_file.is_file():
            load_dotenv(env_file, override=False)
            log_info(f"Config: loaded environment variables from {env_file}")
            break

    if not path.is_file():
        raise ConfigError(
            f"Configuration file not found: {path}. "
            "Copy config.example.yaml to config.yaml and configure it."
        )

    root = (launch_directory or Path.cwd()).resolve()
    state_path = root / RUNTIME_STATE_RELATIVE_PATH
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("Configuration must be a YAML object")
    # Runtime state restores interactive preferences, while explicit ARIA_*
    # environment variables remain the highest-precedence override.
    raw = _apply_runtime_state(raw, _load_runtime_state(state_path))
    raw = _env_override(raw)

    provider = raw.get("provider")
    model = raw.get("model")
    if not isinstance(provider, str) or not provider.strip():
        raise ConfigError("provider is required")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("model is required")

    raw_providers = raw.get("providers", {})
    if not isinstance(raw_providers, dict):
        raise ConfigError("providers must be a YAML object")
    providers: dict[str, dict[str, Any]] = {}
    for name, values in raw_providers.items():
        if not isinstance(name, str) or not isinstance(values, dict):
            raise ConfigError("Each provider configuration must be an object")
        providers[name] = dict(values)
    if provider not in providers and provider not in KNOWN_PROVIDERS:
        raise ConfigError(f"No configuration found for provider: {provider}")

    workspace_value = raw.get("workspace", ".")
    if not isinstance(workspace_value, str):
        raise ConfigError("workspace must be a path")
    workspace = (root / workspace_value).resolve()
    if not workspace.is_dir():
        raise ConfigError(f"Workspace directory does not exist: {workspace}")

    max_iterations = raw.get("max_iterations", 20)
    if not isinstance(max_iterations, int) or max_iterations <= 0:
        raise ConfigError("max_iterations must be a positive integer")

    log_section = raw.get("logging", {}) or {}
    if not isinstance(log_section, dict):
        raise ConfigError("logging must be a YAML object")
    log_directory = Path(str(log_section.get("directory", "data/logs")))
    log_max_bytes = log_section.get("max_bytes", 5 * 1024 * 1024)
    log_backup_count = log_section.get("backup_count", 3)
    if not isinstance(log_max_bytes, int) or log_max_bytes <= 0:
        raise ConfigError("logging.max_bytes must be a positive integer")
    if not isinstance(log_backup_count, int) or log_backup_count < 0:
        raise ConfigError("logging.backup_count must be a non-negative integer")
    logging_config = LoggingConfig(
        directory=log_directory,
        console_level=str(log_section.get("console_level", "WARNING")).upper(),
        max_bytes=log_max_bytes,
        backup_count=log_backup_count,
    )

    speech_section = raw.get("speech", {}) or {}
    if not isinstance(speech_section, dict):
        raise ConfigError("speech must be a YAML object")
    engine = str(speech_section.get("engine", "kokoro")).lower()
    if engine == "kokoro":
        # Backward-compatible alias for the original hosted Kokoro mode.
        engine = "kokoro_hf"
    if engine not in {"kokoro_hf", "kokoro_local", "chatterbox"}:
        raise ConfigError("speech.engine must be 'kokoro_hf', 'kokoro_local', or 'chatterbox'")
    speed = speech_section.get("speed", 1.0)
    if not isinstance(speed, (int, float)) or speed <= 0:
        raise ConfigError("speech.speed must be a positive number")
    speech_config = SpeechConfig(
        enabled=bool(speech_section.get("enabled", False)),
        engine=engine,
        voice=str(speech_section.get("voice", "af_sarah")),
        speed=float(speed),
        lang_code=str(speech_section.get("lang_code", "a")),
        hf_api_key_env=str(speech_section.get("hf_api_key_env", "HF_TOKEN")),
        hf_model=str(speech_section.get("hf_model", "hexgrad/Kokoro-82M")),
        hf_provider=str(speech_section.get("hf_provider", "auto")),
    )

    coder_section = raw.get("coder", {}) or {}
    if not isinstance(coder_section, dict):
        raise ConfigError("coder must be a YAML object")
    coder_iterations = coder_section.get("max_iterations", 60)
    coder_output_chars = coder_section.get("max_output_chars", 12_000)
    if not isinstance(coder_iterations, int) or coder_iterations <= 0:
        raise ConfigError("coder.max_iterations must be a positive integer")
    if not isinstance(coder_output_chars, int) or coder_output_chars <= 0:
        raise ConfigError("coder.max_output_chars must be a positive integer")
    coder_config = CoderConfig(
        provider=(str(coder_section["provider"]).strip() if coder_section.get("provider") is not None else None),
        model=(str(coder_section["model"]).strip() if coder_section.get("model") is not None else None),
        max_iterations=coder_iterations,
        max_output_chars=coder_output_chars,
    )
    if coder_config.provider == "":
        raise ConfigError("coder.provider must be a non-empty provider name or null")
    if coder_config.model == "":
        raise ConfigError("coder.model must be a non-empty model name or null")
    if (
        coder_config.provider is not None
        and coder_config.provider not in providers
        and coder_config.provider not in KNOWN_PROVIDERS
    ):
        raise ConfigError(f"No configuration found for coder provider: {coder_config.provider}")

    persona = str(raw.get("persona", "jarvis"))

    web_section = raw.get("web", {}) or {}
    if not isinstance(web_section, dict):
        raise ConfigError("web must be a YAML object")
    web_timeout = web_section.get("search_timeout", 10)
    page_timeout = web_section.get("page_timeout", 15)
    if not isinstance(web_timeout, (int, float)) or web_timeout <= 0:
        raise ConfigError("web.search_timeout must be a positive number")
    if not isinstance(page_timeout, (int, float)) or page_timeout <= 0:
        raise ConfigError("web.page_timeout must be a positive number")
    web_int_defaults = {
        "max_results": 5,
        "max_search_calls": 4,
        "max_page_fetches": 8,
        "max_total_web_calls": 12,
        "max_page_chars": 30_000,
        "max_snippet_chars": 2_000,
        "max_concurrent_page_fetches": 3,
        "max_retries": 2,
        "search_cache_ttl": 300,
        "page_cache_ttl": 1_800,
    }
    web_ints: dict[str, int] = {}
    for name, default in web_int_defaults.items():
        value = web_section.get(name, default)
        minimum = 0 if name.endswith("ttl") else 1
        if not isinstance(value, int) or value < minimum:
            raise ConfigError(f"web.{name} must be an integer >= {minimum}")
        web_ints[name] = value
    raw_allowed_hosts = web_section.get("allowed_hosts", [])
    if not isinstance(raw_allowed_hosts, list) or not all(isinstance(item, str) and item.strip() for item in raw_allowed_hosts):
        raise ConfigError("web.allowed_hosts must be a list of hostnames")
    searxng_url = str(web_section.get("searxng_url", "http://127.0.0.1:8080")).strip().rstrip("/")
    if not searxng_url:
        raise ConfigError("web.searxng_url must be a non-empty URL")
    web_config = WebConfig(
        enabled=bool(web_section.get("enabled", True)),
        searxng_url=searxng_url,
        search_timeout=float(web_timeout),
        page_timeout=float(page_timeout),
        allowed_hosts=tuple(item.strip() for item in raw_allowed_hosts),
        user_agent=str(web_section.get("user_agent", "ARIA/1.0")),
        **web_ints,
    )

    memory_section = raw.get("memory", {}) or {}
    if not isinstance(memory_section, dict):
        raise ConfigError("memory must be a YAML object")
    memory_directory = Path(str(memory_section.get("directory", "data/memory")))
    chroma_directory = Path(str(memory_section.get("chroma_directory", "data/memory/chroma")))
    fallback_models = memory_section.get(
        "embedding_fallback_models",
        [
            "qwen3-embedding:0.6b",
            "nomic-embed-text",
            "mxbai-embed-large",
            "bge-m3",
            "snowflake-arctic-embed",
        ],
    )
    if not isinstance(fallback_models, list) or not all(isinstance(item, str) and item.strip() for item in fallback_models):
        raise ConfigError("memory.embedding_fallback_models must be a non-empty list of model names")
    int_defaults = {
        "raw_retention_days": 30,
        "archive_retention_days": 365,
        "tier2_retention_days": 730,
        "context_token_budget": 4000,
        "tier2_limit": 8,
        "tier3_limit": 8,
        "promotion_repetitions": 2,
        "promotion_recency_days": 365,
        "retry_interval_seconds": 60,
        "warmup_turns": 1,
    }
    memory_ints: dict[str, int] = {}
    for name, default in int_defaults.items():
        value = memory_section.get(name, default)
        if not isinstance(value, int) or value <= 0:
            raise ConfigError(f"memory.{name} must be a positive integer")
        memory_ints[name] = value
    detail_mode = str(memory_section.get("detail_mode", "raw_then_summary")).lower()
    if detail_mode not in {"raw", "raw_then_summary", "delete_after_summary"}:
        raise ConfigError("memory.detail_mode must be 'raw', 'raw_then_summary', or 'delete_after_summary'")
    rank_names = ("rank_similarity", "rank_recency", "rank_frequency", "rank_importance", "rank_explicit")
    ranks: dict[str, float] = {}
    for name in rank_names:
        value = memory_section.get(name, getattr(MemoryConfig, name))
        if not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"memory.{name} must be a non-negative number")
        ranks[name] = float(value)
    if sum(ranks.values()) <= 0:
        raise ConfigError("memory ranking weights must not all be zero")
    threshold_names = ("promotion_confidence", "promotion_importance")
    thresholds: dict[str, float] = {}
    for name in threshold_names:
        value = memory_section.get(name, getattr(MemoryConfig, name))
        if not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ConfigError(f"memory.{name} must be between 0 and 1")
        thresholds[name] = float(value)
    memory_config = MemoryConfig(
        enabled=bool(memory_section.get("enabled", True)),
        directory=memory_directory,
        chroma_directory=chroma_directory,
        embedding_base_url=str(memory_section.get("embedding_base_url", "")).strip().rstrip("/"),
        embedding_model=str(memory_section.get("embedding_model", "")).strip(),
        embedding_api_key_env=str(memory_section.get("embedding_api_key_env", "MEMORY_EMBEDDING_API_KEY")),
        embedding_ollama_host=str(memory_section.get("embedding_ollama_host", "http://127.0.0.1:11434")),
        embedding_fallback_models=tuple(fallback_models),
        detail_mode=detail_mode,
        rank_similarity=ranks["rank_similarity"],
        rank_recency=ranks["rank_recency"],
        rank_frequency=ranks["rank_frequency"],
        rank_importance=ranks["rank_importance"],
        rank_explicit=ranks["rank_explicit"],
        promotion_confidence=thresholds["promotion_confidence"],
        promotion_importance=thresholds["promotion_importance"],
        raw_retention_days=memory_ints["raw_retention_days"],
        archive_retention_days=memory_ints["archive_retention_days"],
        tier2_retention_days=memory_ints["tier2_retention_days"],
        context_token_budget=memory_ints["context_token_budget"],
        tier2_limit=memory_ints["tier2_limit"],
        tier3_limit=memory_ints["tier3_limit"],
        promotion_repetitions=memory_ints["promotion_repetitions"],
        promotion_recency_days=memory_ints["promotion_recency_days"],
        retry_interval_seconds=memory_ints["retry_interval_seconds"],
        warmup_turns=memory_ints["warmup_turns"],
    )

    ui_section = raw.get("ui", {}) or {}
    if not isinstance(ui_section, dict):
        raise ConfigError("ui must be a YAML object")
    show_cot = ui_section.get("show_cot", True)
    if not isinstance(show_cot, bool):
        raise ConfigError("ui.show_cot must be a boolean")

    lifeos_section = raw.get("lifeos", {}) or {}
    if not isinstance(lifeos_section, dict):
        raise ConfigError("lifeos must be a YAML object")
    timeout = _optional_positive_number(lifeos_section.get("timeout_seconds"), "lifeos.timeout_seconds")
    max_retries = lifeos_section.get("max_retries", 1)
    if not isinstance(max_retries, int) or max_retries < 0:
        raise ConfigError("lifeos.max_retries must be a non-negative integer")
    backoff = _optional_positive_number(lifeos_section.get("retry_backoff_seconds"), "lifeos.retry_backoff_seconds")
    lifeos_config = LifeOSConfig(
        enabled=bool(lifeos_section.get("enabled", True)),
        base_url=str(lifeos_section.get("base_url", "")).strip().rstrip("/"),
        api_key_env=str(lifeos_section.get("api_key_env", "LIFEOS_API_KEY")),
        timeout_seconds=float(timeout) if timeout is not None else 15.0,
        max_retries=max_retries,
        retry_backoff_seconds=float(backoff) if backoff is not None else 0.4,
    )

    log_debug(
        f"Config: provider={provider} model={model} workspace={workspace} "
        f"max_iterations={max_iterations} persona={persona}"
    )
    return AppConfig(
        provider=provider,
        model=model,
        providers=providers,
        workspace=workspace,
        max_iterations=max_iterations,
        command_timeout_seconds=_optional_positive_number(
            raw.get("command_timeout_seconds"), "command_timeout_seconds"
        ),
        max_command_output_chars=_optional_positive_int(
            raw.get("max_command_output_chars"), "max_command_output_chars"
        ),
        persona=persona,
        logging=logging_config,
        speech=speech_config,
        coder=coder_config,
        lifeos=lifeos_config,
        web=web_config,
        memory=memory_config,
        show_cot=show_cot,
        runtime_state_path=state_path,
    )
