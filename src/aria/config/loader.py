"""Runtime configuration loading (YAML + .env overrides)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from ..logging.setup import log_debug, log_error, log_info


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
    "zai",
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
    # Opt-in: let ARIA run `docker compose up -d` for SearXNG when it is not
    # reachable at startup. Off by default so startup never changes Docker
    # state and never blocks on an optional service (bugReport BR-1).
    start_backend: bool = False


@dataclass(frozen=True)
class BrowserConfig:
    """Persistent Playwright browser settings."""

    enabled: bool = False
    headless: bool = False
    profile_directory: Path = Path("data/browser-profile")
    executable_path: str = ""
    timeout_ms: int = 15_000
    max_text_chars: int = 30_000
    wait_until: str = "domcontentloaded"
    viewport_width: int = 1440
    viewport_height: int = 900
    launch_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class DesktopConfig:
    """Hyprland control settings; managed mode is the safe default."""

    enabled: bool = False
    mode: str = "managed"  # managed | unrestricted
    input_backend: str = "arch"  # arch | pyautogui
    hyprctl_command: str = "hyprctl"
    wtype_command: str = "wtype"
    ydotool_command: str = "ydotool"
    screenshot_command: str = "grim"
    hyprland_config_path: str = ""
    command_timeout_seconds: float = 15.0
    launchers: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class NotificationConfig:
    """Desktop notification and optional speech settings for background work."""

    enabled: bool = True
    backend: str = "notify-send"  # notify-send | dbus
    notify_send_command: str = "notify-send"
    dbus_command: str = "gdbus"
    app_name: str = "ARIA"
    default_urgency: str = "normal"
    default_timeout_ms: int = 10_000
    tts_enabled: bool = True


@dataclass(frozen=True)
class AutonomyConfig:
    """Categories ARIA may execute without an interactive prompt."""

    enabled: bool = False
    allowed_categories: tuple[str, ...] = ()
    lifeos_write_operations: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackgroundConfig:
    """Dedicated model settings for proactive analysis and briefings."""

    enabled: bool = True
    provider: str | None = None
    model: str | None = None
    api_key_env: str = ""
    profile_path: Path = Path("data/proactive-profile.json")
    max_output_chars: int = 8_000


@dataclass(frozen=True)
class VisionConfig:
    """Image input/generation and visual fallback settings."""

    enabled: bool = True
    fallback_provider: str | None = None
    fallback_model: str | None = None
    fallback_api_key_env: str = ""
    max_image_bytes: int = 10 * 1024 * 1024
    clipboard_backend: str = "auto"  # auto | wl-paste | xclip | python
    screenshot_command: str = "grim"
    retain_images: bool = False
    periodic_screen_enabled: bool = False
    periodic_screen_cron: str = "*/15 * * * *"


@dataclass(frozen=True)
class SchedulerConfig:
    """In-process persistent scheduler settings."""

    enabled: bool = False
    # Read-only briefings/monitoring are independent from consequential
    # autonomy permissions and are enabled by default when scheduling is on.
    analysis_enabled: bool = True
    database: Path = Path("data/scheduler/scheduler.sqlite3")
    workflow_directory: Path = Path("workflows")
    poll_seconds: int = 15
    default_workflows: bool = True
    timezone: str = "system"
    default_misfire_policy: str = "skip"  # skip | run_once
    briefing_times: tuple[str, ...] = ("08:00", "13:00", "18:00")
    deadline_check_minutes: int = 30
    goal_check_minutes: int = 120
    calendar_check_minutes: int = 60
    briefing_web_queries: tuple[str, ...] = ()


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
    """Hybrid memory settings (SQLite + Chroma RAG).

    Field set matches the new ``aria.memory`` engine exactly: every field here
    is consumed, and nothing the engine needs is missing.
    """

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
    context_token_budget: int = 4000
    rank_similarity: float = 0.25
    rank_recency: float = 0.15
    rank_frequency: float = 0.10
    rank_importance: float = 0.35
    rank_confidence: float = 0.15
    episodic_retention_days: int = 180
    conversation_retention_days: int = 365
    knowledge_retention_days: int = 0  # 0 = never expires


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
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    desktop: DesktopConfig = field(default_factory=DesktopConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    autonomy: AutonomyConfig = field(default_factory=AutonomyConfig)
    background: BackgroundConfig = field(default_factory=BackgroundConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    show_cot: bool = True
    keep_cot: bool = False
    ui_backend: str = "urwid"
    runtime_state_path: Path = Path("data/state/aria-state.yaml")
    # Top-level keys the runtime state file overrode (empty when none or when
    # state loading was skipped). Surfaced so startup can show the source of
    # the effective provider/model (bugReport BR-2).
    runtime_state_overrides: tuple[str, ...] = ()


RUNTIME_STATE_RELATIVE_PATH = Path("data/state/aria-state.yaml")


def _load_runtime_state(path: Path, root: Path) -> dict[str, Any]:
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

    # Sanitize before applying: one stale or corrupt value (e.g. a workspace
    # under a since-deleted pytest tmpdir, or a scratch model name) must drop
    # that single key instead of blocking startup (bugReport BR-2).
    for key in ("provider", "model"):
        value = state.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            log_error(f"Config: ignoring invalid runtime state {key}: {value!r}")
            del state[key]
    workspace = state.get("workspace")
    if workspace is not None:
        valid = isinstance(workspace, str) and workspace.strip() and (root / workspace).resolve().is_dir()
        if valid:
            state["workspace"] = str(workspace)
        else:
            log_error(f"Config: ignoring runtime state workspace (missing directory): {workspace!r}")
            del state["workspace"]
    iterations = state.get("max_iterations")
    if iterations is not None and (not isinstance(iterations, int) or isinstance(iterations, bool) or iterations <= 0):
        log_error(f"Config: ignoring invalid runtime state max_iterations: {iterations!r}")
        del state["max_iterations"]
    return state


def _apply_runtime_state(raw: dict[str, Any], state: dict[str, Any]) -> set[str]:
    """Apply saved runtime preferences; return the keys that were overridden."""
    applied: set[str] = set()
    for key in ("provider", "model", "workspace", "max_iterations"):
        if key in state:
            raw[key] = state[key]
            applied.add(key)
    for section in ("speech", "coder", "ui"):
        values = state.get(section)
        if not isinstance(values, dict) or not values:
            continue
        current = raw.get(section)
        if not isinstance(current, dict):
            current = {}
        current.update(values)
        raw[section] = current
        applied.add(section)
    return applied


def save_runtime_state(
    path: Path,
    config: AppConfig,
    *,
    show_cot: bool | None = None,
    keep_cot: bool | None = None,
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
        "ui": {
            "show_cot": config.show_cot if show_cot is None else show_cot,
            "keep_cot": config.keep_cot if keep_cot is None else keep_cot,
        },
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


def load_config(
    path: Path,
    launch_directory: Path | None = None,
    *,
    ignore_runtime_state: bool = False,
) -> AppConfig:
    """Load config from YAML (+.env), resolving the workspace from the launch directory.

    With ``ignore_runtime_state`` the persisted REPL preferences in
    ``data/state/aria-state.yaml`` are skipped entirely (the ``--ignore-state``
    flag), so static ``config.yaml`` values win.
    """
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
    runtime_overrides: set[str] = set()
    if ignore_runtime_state:
        log_info("Config: runtime state ignored (--ignore-state)")
    else:
        state = _load_runtime_state(state_path, root)
        runtime_overrides = _apply_runtime_state(raw, state)
        if runtime_overrides:
            log_info(f"Config: runtime state overrides applied: {', '.join(sorted(runtime_overrides))}")
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
        start_backend=bool(web_section.get("start_backend", False)),
        **web_ints,
    )

    browser_section = raw.get("browser", {}) or {}
    if not isinstance(browser_section, dict):
        raise ConfigError("browser must be a YAML object")
    browser_timeout = browser_section.get("timeout_ms", 15_000)
    browser_text_limit = browser_section.get("max_text_chars", 30_000)
    viewport_width = browser_section.get("viewport_width", 1440)
    viewport_height = browser_section.get("viewport_height", 900)
    if not isinstance(browser_timeout, int) or browser_timeout <= 0:
        raise ConfigError("browser.timeout_ms must be a positive integer")
    if not isinstance(browser_text_limit, int) or browser_text_limit <= 0:
        raise ConfigError("browser.max_text_chars must be a positive integer")
    if not isinstance(viewport_width, int) or viewport_width <= 0 or not isinstance(viewport_height, int) or viewport_height <= 0:
        raise ConfigError("browser viewport dimensions must be positive integers")
    wait_until = str(browser_section.get("wait_until", "domcontentloaded")).strip().lower()
    if wait_until not in {"commit", "domcontentloaded", "load", "networkidle"}:
        raise ConfigError("browser.wait_until must be commit, domcontentloaded, load, or networkidle")
    launch_args = browser_section.get("launch_args", [])
    if not isinstance(launch_args, list) or not all(isinstance(item, str) for item in launch_args):
        raise ConfigError("browser.launch_args must be a list of strings")
    browser_profile = Path(str(browser_section.get("profile_directory", "data/browser-profile")))
    if not browser_profile.is_absolute():
        browser_profile = root / browser_profile
    browser_config = BrowserConfig(
        enabled=bool(browser_section.get("enabled", False)),
        headless=bool(browser_section.get("headless", False)),
        profile_directory=browser_profile,
        executable_path=str(browser_section.get("executable_path", "")).strip(),
        timeout_ms=browser_timeout,
        max_text_chars=browser_text_limit,
        wait_until=wait_until,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        launch_args=tuple(launch_args),
    )

    desktop_section = raw.get("desktop", {}) or {}
    if not isinstance(desktop_section, dict):
        raise ConfigError("desktop must be a YAML object")
    desktop_mode = str(desktop_section.get("mode", "managed")).strip().lower()
    if desktop_mode not in {"managed", "unrestricted"}:
        raise ConfigError("desktop.mode must be managed or unrestricted")
    input_backend = str(desktop_section.get("input_backend", "arch")).strip().lower()
    if input_backend not in {"arch", "pyautogui"}:
        raise ConfigError("desktop.input_backend must be arch or pyautogui")
    desktop_timeout = desktop_section.get("command_timeout_seconds", 15.0)
    if not isinstance(desktop_timeout, (int, float)) or desktop_timeout <= 0:
        raise ConfigError("desktop.command_timeout_seconds must be a positive number")
    raw_launchers = desktop_section.get("launchers", {}) or {}
    if not isinstance(raw_launchers, dict):
        raise ConfigError("desktop.launchers must be a YAML object")
    launchers: dict[str, tuple[str, ...]] = {}
    for name, command in raw_launchers.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ConfigError("desktop.launchers must map names to non-empty command arrays")
        launchers[name.strip()] = tuple(command)
    desktop_config = DesktopConfig(
        enabled=bool(desktop_section.get("enabled", False)),
        mode=desktop_mode,
        input_backend=input_backend,
        hyprctl_command=str(desktop_section.get("hyprctl_command", "hyprctl")),
        wtype_command=str(desktop_section.get("wtype_command", "wtype")),
        ydotool_command=str(desktop_section.get("ydotool_command", "ydotool")),
        screenshot_command=str(desktop_section.get("screenshot_command", "grim")),
        hyprland_config_path=str(desktop_section.get("hyprland_config_path", "")).strip(),
        command_timeout_seconds=float(desktop_timeout),
        launchers=launchers,
    )

    notifications_section = raw.get("notifications", {}) or {}
    if not isinstance(notifications_section, dict):
        raise ConfigError("notifications must be a YAML object")
    urgency = str(notifications_section.get("default_urgency", "normal")).lower()
    if urgency not in {"low", "normal", "critical"}:
        raise ConfigError("notifications.default_urgency must be low, normal, or critical")
    notification_backend = str(notifications_section.get("backend", "notify-send")).lower()
    if notification_backend not in {"notify-send", "dbus"}:
        raise ConfigError("notifications.backend must be notify-send or dbus")
    notification_timeout = notifications_section.get("default_timeout_ms", 10_000)
    if not isinstance(notification_timeout, int) or notification_timeout < 0:
        raise ConfigError("notifications.default_timeout_ms must be a non-negative integer")
    notifications_config = NotificationConfig(
        enabled=bool(notifications_section.get("enabled", True)),
        backend=notification_backend,
        notify_send_command=str(notifications_section.get("notify_send_command", "notify-send")),
        dbus_command=str(notifications_section.get("dbus_command", "gdbus")),
        app_name=str(notifications_section.get("app_name", "ARIA")),
        default_urgency=urgency,
        default_timeout_ms=notification_timeout,
        tts_enabled=bool(notifications_section.get("tts_enabled", True)),
    )

    autonomy_section = raw.get("autonomy", {}) or {}
    if not isinstance(autonomy_section, dict):
        raise ConfigError("autonomy must be a YAML object")
    allowed_categories = autonomy_section.get("allowed_categories", [])
    valid_categories = {"analysis", "lifeos_writes", "notifications", "computer_control", "timers"}
    if not isinstance(allowed_categories, list) or not all(isinstance(item, str) and item in valid_categories for item in allowed_categories):
        raise ConfigError(f"autonomy.allowed_categories must contain only: {', '.join(sorted(valid_categories))}")
    lifeos_write_operations = autonomy_section.get("lifeos_write_operations", [])
    if not isinstance(lifeos_write_operations, list) or not all(isinstance(item, str) and item.strip() for item in lifeos_write_operations):
        raise ConfigError("autonomy.lifeos_write_operations must be a list of operation names")
    autonomy_config = AutonomyConfig(
        enabled=bool(autonomy_section.get("enabled", False)),
        allowed_categories=tuple(allowed_categories),
        lifeos_write_operations=tuple(lifeos_write_operations),
    )

    background_section = raw.get("background", {}) or {}
    if not isinstance(background_section, dict):
        raise ConfigError("background must be a YAML object")
    background_output = background_section.get("max_output_chars", 8_000)
    if not isinstance(background_output, int) or background_output <= 0:
        raise ConfigError("background.max_output_chars must be a positive integer")
    background_profile = Path(str(background_section.get("profile_path", "data/proactive-profile.json")))
    if not background_profile.is_absolute():
        background_profile = root / background_profile
    background_config = BackgroundConfig(
        enabled=bool(background_section.get("enabled", True)),
        provider=(str(background_section["provider"]).strip() if background_section.get("provider") is not None else None),
        model=(str(background_section["model"]).strip() if background_section.get("model") is not None else None),
        api_key_env=str(background_section.get("api_key_env", "")).strip(),
        profile_path=background_profile,
        max_output_chars=background_output,
    )
    if background_config.provider == "":
        raise ConfigError("background.provider must be a provider name or null")
    if background_config.model == "":
        raise ConfigError("background.model must be a model name or null")
    if background_config.provider is not None and background_config.provider not in providers and background_config.provider not in KNOWN_PROVIDERS:
        raise ConfigError(f"No configuration found for background provider: {background_config.provider}")

    vision_section = raw.get("vision", {}) or {}
    if not isinstance(vision_section, dict):
        raise ConfigError("vision must be a YAML object")
    vision_backend = str(vision_section.get("clipboard_backend", "auto")).lower()
    if vision_backend not in {"auto", "wl-paste", "xclip", "python"}:
        raise ConfigError("vision.clipboard_backend must be auto, wl-paste, xclip, or python")
    max_image_bytes = vision_section.get("max_image_bytes", 10 * 1024 * 1024)
    if not isinstance(max_image_bytes, int) or max_image_bytes <= 0:
        raise ConfigError("vision.max_image_bytes must be a positive integer")
    periodic_screen_cron = str(vision_section.get("periodic_screen_cron", "*/15 * * * *")).strip()
    if len(periodic_screen_cron.split()) != 5:
        raise ConfigError("vision.periodic_screen_cron must be a five-field cron expression")
    vision_config = VisionConfig(
        enabled=bool(vision_section.get("enabled", True)),
        fallback_provider=(str(vision_section["fallback_provider"]).strip() if vision_section.get("fallback_provider") is not None else None),
        fallback_model=(str(vision_section["fallback_model"]).strip() if vision_section.get("fallback_model") is not None else None),
        fallback_api_key_env=str(vision_section.get("fallback_api_key_env", "")).strip(),
        max_image_bytes=max_image_bytes,
        clipboard_backend=vision_backend,
        screenshot_command=str(vision_section.get("screenshot_command", "grim")),
        retain_images=bool(vision_section.get("retain_images", False)),
        periodic_screen_enabled=bool(vision_section.get("periodic_screen_enabled", False)),
        periodic_screen_cron=periodic_screen_cron,
    )
    if vision_config.fallback_provider == "":
        raise ConfigError("vision.fallback_provider must be a provider name or null")
    if vision_config.fallback_model == "":
        raise ConfigError("vision.fallback_model must be a model name or null")

    scheduler_section = raw.get("scheduler", {}) or {}
    if not isinstance(scheduler_section, dict):
        raise ConfigError("scheduler must be a YAML object")
    poll_seconds = scheduler_section.get("poll_seconds", 15)
    if not isinstance(poll_seconds, int) or poll_seconds <= 0:
        raise ConfigError("scheduler.poll_seconds must be a positive integer")
    misfire_policy = str(scheduler_section.get("default_misfire_policy", "skip")).lower()
    if misfire_policy not in {"skip", "run_once"}:
        raise ConfigError("scheduler.default_misfire_policy must be skip or run_once")
    interval_values: dict[str, int] = {}
    for name, default in (("deadline_check_minutes", 30), ("goal_check_minutes", 120), ("calendar_check_minutes", 60)):
        value = scheduler_section.get(name, default)
        if not isinstance(value, int) or value <= 0:
            raise ConfigError(f"scheduler.{name} must be a positive integer")
        interval_values[name] = value
    briefing_times = scheduler_section.get("briefing_times", ["08:00", "13:00", "18:00"])
    if len(briefing_times) != 3 or not all(isinstance(item, str) and len(item) == 5 and item[2] == ":" for item in briefing_times):
        raise ConfigError("scheduler.briefing_times must contain three HH:MM strings")
    for item in briefing_times:
        try:
            hour, minute = (int(value) for value in item.split(":", 1))
        except ValueError as exc:
            raise ConfigError("scheduler.briefing_times must contain valid HH:MM values") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ConfigError("scheduler.briefing_times must contain valid HH:MM values")
    scheduler_database = Path(str(scheduler_section.get("database", "data/scheduler/scheduler.sqlite3")))
    workflow_directory = Path(str(scheduler_section.get("workflow_directory", "workflows")))
    if not scheduler_database.is_absolute():
        scheduler_database = root / scheduler_database
    if not workflow_directory.is_absolute():
        workflow_directory = root / workflow_directory
    web_queries = scheduler_section.get("briefing_web_queries", [])
    if not isinstance(web_queries, list) or not all(isinstance(item, str) for item in web_queries):
        raise ConfigError("scheduler.briefing_web_queries must be a list of strings")
    if str(scheduler_section.get("timezone", "system")).strip().lower() != "system":
        raise ConfigError("scheduler.timezone currently supports only system local time")
    scheduler_config = SchedulerConfig(
        enabled=bool(scheduler_section.get("enabled", False)),
        analysis_enabled=bool(scheduler_section.get("analysis_enabled", True)),
        database=scheduler_database,
        workflow_directory=workflow_directory,
        poll_seconds=poll_seconds,
        default_workflows=bool(scheduler_section.get("default_workflows", True)),
        timezone=str(scheduler_section.get("timezone", "system")),
        default_misfire_policy=misfire_policy,
        briefing_times=tuple(briefing_times),
        briefing_web_queries=tuple(web_queries),
        **interval_values,
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
        "context_token_budget": 4000,
        "episodic_retention_days": 180,
        "conversation_retention_days": 365,
        "knowledge_retention_days": 0,
    }
    memory_ints: dict[str, int] = {}
    for name, default in int_defaults.items():
        value = memory_section.get(name, default)
        if not isinstance(value, int) or value < 0:
            raise ConfigError(f"memory.{name} must be a non-negative integer")
        memory_ints[name] = value
    rank_names = ("rank_similarity", "rank_recency", "rank_frequency", "rank_importance", "rank_confidence")
    ranks: dict[str, float] = {}
    for name in rank_names:
        value = memory_section.get(name, getattr(MemoryConfig, name))
        if not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"memory.{name} must be a non-negative number")
        ranks[name] = float(value)
    if sum(ranks.values()) <= 0:
        raise ConfigError("memory ranking weights must not all be zero")
    memory_config = MemoryConfig(
        enabled=bool(memory_section.get("enabled", True)),
        directory=memory_directory,
        chroma_directory=chroma_directory,
        embedding_base_url=str(memory_section.get("embedding_base_url", "")).strip().rstrip("/"),
        embedding_model=str(memory_section.get("embedding_model", "")).strip(),
        embedding_api_key_env=str(memory_section.get("embedding_api_key_env", "MEMORY_EMBEDDING_API_KEY")),
        embedding_ollama_host=str(memory_section.get("embedding_ollama_host", "http://127.0.0.1:11434")),
        embedding_fallback_models=tuple(fallback_models),
        rank_similarity=ranks["rank_similarity"],
        rank_recency=ranks["rank_recency"],
        rank_frequency=ranks["rank_frequency"],
        rank_importance=ranks["rank_importance"],
        rank_confidence=ranks["rank_confidence"],
        episodic_retention_days=memory_ints["episodic_retention_days"],
        conversation_retention_days=memory_ints["conversation_retention_days"],
        knowledge_retention_days=memory_ints["knowledge_retention_days"],
        context_token_budget=memory_ints["context_token_budget"],
    )

    ui_section = raw.get("ui", {}) or {}
    if not isinstance(ui_section, dict):
        raise ConfigError("ui must be a YAML object")
    show_cot = ui_section.get("show_cot", True)
    if not isinstance(show_cot, bool):
        raise ConfigError("ui.show_cot must be a boolean")
    keep_cot = ui_section.get("keep_cot", False)
    if not isinstance(keep_cot, bool):
        raise ConfigError("ui.keep_cot must be a boolean")
    ui_backend = str(ui_section.get("backend", "urwid")).strip().lower()
    if ui_backend not in {"urwid", "rich"}:
        raise ConfigError("ui.backend must be 'urwid' or 'rich'")

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
        browser=browser_config,
        desktop=desktop_config,
        notifications=notifications_config,
        autonomy=autonomy_config,
        background=background_config,
        vision=vision_config,
        scheduler=scheduler_config,
        show_cot=show_cot,
        keep_cot=keep_cot,
        ui_backend=ui_backend,
        runtime_state_path=state_path,
        runtime_state_overrides=tuple(sorted(runtime_overrides)),
    )
