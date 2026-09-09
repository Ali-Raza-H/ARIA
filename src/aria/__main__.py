"""ARIA command-line entrypoint."""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from rich.console import Console

from .agent.aria import AriaAgent, register_deploy_coder_tool
from .agent.coder import CoderService
from .config import RUNTIME_STATE_RELATIVE_PATH, AppConfig, ConfigError, WebConfig, load_config
from .llm.base import list_models
from .images import capture_clipboard, capture_screen, load_image, register_image_tools
from .notifications import NotificationService
from .proactive import ProactiveService
from .scheduler import ScheduledJob, SchedulerService, register_scheduler_tools
from .llm.factory import ProviderManager
from .logging_setup import configure_logging, log_error, log_info
from .memory import MemoryManager, MemoryError, MemoryStore, MemorySettings, SessionMemory
from .memory.tools import register_memory_tools
from .skills import SkillManager
from .speech import SpeechController
from .tools import (
    BrowserToolService,
    DesktopToolService,
    ToolContext,
    ToolRegistry,
    register_browser_tools,
    register_desktop_tools,
    register_filesystem_tools,
    register_lifeos_tool,
    register_shell_tool,
    WebToolService,
    register_web_tools,
)
from .tools.web import SearXNGProvider
from .ui.factory import create_repl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ARIA - Adaptive Reasoning and Intelligence Assistant")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to the YAML configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--ignore-state",
        action="store_true",
        help="Ignore data/state/aria-state.yaml for this run; config.yaml wins",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Delete data/state/aria-state.yaml before starting, then behave like --ignore-state",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    console = Console()
    launch_directory = Path.cwd()

    if args.reset_state:
        state_file = launch_directory / RUNTIME_STATE_RELATIVE_PATH
        try:
            state_file.unlink(missing_ok=True)
            console.print(f"Runtime state cleared: {state_file}", style="yellow")
        except OSError as exc:
            console.print(f"Could not clear runtime state {state_file}: {exc}", style="red")
            return 1

    try:
        config = load_config(args.config, launch_directory, ignore_runtime_state=args.ignore_state)
    except (ConfigError, ValueError, OSError) as exc:
        console.print(f"Configuration error: {exc}", style="red")
        return 1

    configure_logging(
        launch_directory / config.logging.directory,
        console_level=getattr(logging, config.logging.console_level, logging.WARNING),
        max_bytes=config.logging.max_bytes,
        backup_count=config.logging.backup_count,
    )
    log_info(f"ARIA starting: provider={config.provider} model={config.model}")

    try:
        provider_manager = ProviderManager(config.providers)
        # BR-2: a stale provider/model from data/state/aria-state.yaml must not make
        # ARIA look broken on launch; validate and fall back when unavailable.
        fallback_model = _validate_model_choice(provider_manager, config)
        if fallback_model is not None:
            source = (
                "runtime state"
                if {"model", "provider"} & set(config.runtime_state_overrides)
                else "config.yaml"
            )
            console.print(
                f"Model {config.model!r} (from {source}) is not available on {config.provider}. "
                f"Falling back to {fallback_model!r} for this run — use /model <name> "
                "or edit config.yaml to change it.",
                style="yellow",
            )
            config = replace(config, model=fallback_model)
        provider = provider_manager.create(config.provider, config.model)
        provider_manager.set_active_model(config.provider, config.model)
    except (ValueError, OSError) as exc:
        log_error(f"Startup: provider initialization failed: {exc}")
        console.print(f"Provider error: {exc}", style="red")
        return 1

    # Show the effective choice and where it came from (bugReport BR-2).
    override_note = (
        f"  ·  runtime state: {', '.join(config.runtime_state_overrides)}"
        if config.runtime_state_overrides
        else ""
    )
    console.print(
        f"ARIA · provider={config.provider}  model={config.model}  "
        f"workspace={config.workspace}{override_note}",
        style="dim",
    )

    # ARIA's own toolset: she converses and delegates; heavy tools belong to the coder.
    # ARIA's toolset: she converses, runs quick commands herself, and
    # delegates heavy work to the independent coder agent.
    registry = ToolRegistry()
    register_filesystem_tools(registry)
    register_shell_tool(registry)
    browser_service: BrowserToolService | None = None
    if config.browser.enabled:
        browser_service = BrowserToolService(config.browser, config.workspace)
        register_browser_tools(registry, browser_service)
        log_info("Startup: persistent Playwright browser tools enabled for ARIA")
    desktop_service: DesktopToolService | None = None
    if config.desktop.enabled:
        desktop_service = DesktopToolService(config.desktop)
        register_desktop_tools(registry, desktop_service)
        log_info(f"Startup: Hyprland desktop tools enabled in {config.desktop.mode} mode")
    # LifeOS connection (ported from CIEL): personal-life API for tasks,
    # projects, goals, habits, journal, etc. Self-disables when unconfigured.
    register_lifeos_tool(registry, config.lifeos)
    web_service: WebToolService | None = None
    if config.web.enabled:
        web_provider = SearXNGProvider(
            config.web.searxng_url,
            timeout=config.web.search_timeout,
            max_retries=config.web.max_retries,
            user_agent=config.web.user_agent,
        )
        web_service = WebToolService(web_provider, config.web)
        register_web_tools(registry, web_service)
        log_info(f"Startup: web search configured for {config.web.searxng_url}")

        # BR-1: an optional SearXNG backend must never block startup. Warn and
        # continue; the web tools surface per-call errors until it is up.
        if not _ensure_web_backend(launch_directory, config.web):
            console.print(
                f"Web search backend is not reachable at {config.web.searxng_url}.\n"
                "Continuing without it — web tools will report errors until it is up.\n"
                "Start it with:\n"
                "  docker compose -f infrastructure/searxng/docker-compose.yml up -d\n"
                "or set web.start_backend: true to let ARIA attempt that at startup.",
                style="yellow",
            )
    else:
        log_info("Startup: web search disabled")

    coder_provider = config.coder.provider or config.provider
    coder_model = config.coder.model
    if coder_model is None:
        coder_model = (
            provider_manager.active_model(coder_provider)
            or (provider_manager.models_for(coder_provider) or [config.model])[0]
        )
    coder_service = CoderService(
        provider_manager,
        provider_name=coder_provider,
        model=coder_model,
        max_iterations=config.coder.max_iterations,
        max_output_chars=config.coder.max_output_chars,
    )
    deploy_handler = register_deploy_coder_tool(registry, coder_service)

    background_provider = None
    if config.background.enabled and config.background.provider and config.background.model:
        try:
            background_provider = provider_manager.create_role(
                config.background.provider,
                config.background.model,
                "background",
                config.background.api_key_env,
            )
        except (ValueError, OSError) as exc:
            log_error(f"Startup: background provider unavailable: {exc}")
            console.print(f"Background analysis disabled: {exc}", style="yellow")

    vision_fallback_provider = None
    if config.vision.enabled and config.vision.fallback_provider and config.vision.fallback_model:
        try:
            vision_fallback_provider = provider_manager.create_role(
                config.vision.fallback_provider,
                config.vision.fallback_model,
                "vision",
                config.vision.fallback_api_key_env,
            )
        except (ValueError, OSError) as exc:
            log_error(f"Startup: visual fallback unavailable: {exc}")
            console.print(f"Visual fallback disabled: {exc}", style="yellow")

    pending_image_attachments = []
    register_image_tools(registry, provider, config.vision, pending_image_attachments)

    if config.memory.enabled:
        try:
            memory = MemoryManager(
                launch_directory,
                MemorySettings.from_config(config.memory, launch_directory),
                provider=provider,
            )
        except MemoryError as exc:
            # Memory is an optional feature; it must not block startup (BR-1
            # principle). Fall back to session-only memory instead of exiting.
            log_error(f"Startup: persistent memory initialization failed: {exc}")
            console.print(
                f"Persistent memory unavailable ({exc}); continuing with session-only memory.",
                style="yellow",
            )
            memory = SessionMemory(launch_directory / "data" / "sessions", label="aria")
        else:
            # BR-4: probe embedding health once so degraded mode is detected
            # and announced at startup instead of surfacing as repeated
            # per-turn errors in the logs.
            probe = getattr(memory.embeddings, "probe", None)
            if callable(probe):
                probe()
            if getattr(memory, "degraded", False):
                # BR-4: one visible, low-noise notice instead of repeated
                # embedding errors during every turn.
                console.print(
                    "Persistent memory is in degraded mode: no embedding backend is "
                    "available, so only structured facts/preferences are stored and "
                    "semantic recall is off.\nStart Ollama and pull an embedding model "
                    "(e.g. `ollama pull qwen3-embedding:0.6b`), then restart ARIA.",
                    style="yellow",
                )
            # LLM-callable memory tools (spec §71): remember/search/list/forget.
            register_memory_tools(registry, memory)
    else:
        memory = SessionMemory(launch_directory / "data" / "sessions", label="aria")
    context = ToolContext(
        workspace=config.workspace,
        command_timeout_seconds=config.command_timeout_seconds,
        max_command_output_chars=config.max_command_output_chars,
    )
    speech = SpeechController(config.speech)
    notification_service = NotificationService(config.notifications)
    proactive_service = ProactiveService(
        config.lifeos,
        config.background,
        config.scheduler,
        config.background.profile_path,
        provider=background_provider,
        web_service=web_service,
        workspace=config.workspace,
        vision=config.vision,
        vision_fallback_provider=vision_fallback_provider,
    )
    scheduler_service: SchedulerService | None = None

    def run_autonomous_action(category: str, action: dict) -> dict:
        if category in {"analysis", "lifeos_writes", "notifications"}:
            return proactive_service.run_action(category, action, config.autonomy)
        if category == "computer_control":
            tool_name = action.get("tool")
            arguments = action.get("arguments", {})
            if not isinstance(tool_name, str) or not isinstance(arguments, dict):
                raise ValueError("computer workflow actions require tool and object arguments")
            result = registry.execute(tool_name, arguments, context)
            return {"notification": result.output, "success": not result.is_error}
        if category == "timers":
            if scheduler_service is None:
                raise RuntimeError("scheduler is not active")
            action_type = str(action.get("type", ""))
            if action_type == "create":
                timer_id = scheduler_service.create_timer(
                    str(action.get("name", "scheduled timer")),
                    str(action.get("kind", "reminder")),
                    float(action.get("duration_seconds", 0)),
                    bool(action.get("persistent", True)),
                )
                return {"notification": f"Timer created: {timer_id}", "timer_id": timer_id}
            if action_type == "control":
                return scheduler_service.control_timer(str(action.get("timer_id", "")), str(action.get("command", "")))
            raise ValueError(f"unknown timer workflow action: {action_type}")
        raise ValueError(f"unsupported autonomous category: {category}")

    def notify(title: str, body: str, urgency: str) -> None:
        notification_service.send(title, body, urgency, tts=speech.say if config.notifications.tts_enabled else None)

    scheduler_service = SchedulerService(
        config.scheduler,
        config.autonomy,
        run_autonomous_action,
        notify,
    ) if config.scheduler.enabled else None
    if scheduler_service is not None:
        if config.vision.enabled and config.vision.periodic_screen_enabled:
            # Screen context is deliberately opt-in and uses the same durable
            # scheduler/audit path as every other autonomous analysis.
            scheduler_service.store.upsert_job(
                ScheduledJob(
                    "periodic_screen_context",
                    "periodic_screen_context",
                    config.vision.periodic_screen_cron,
                    "analysis",
                    {"type": "screen_check"},
                    config.scheduler.default_misfire_policy,
                )
            )

        register_scheduler_tools(registry, scheduler_service)
        scheduler_service.start()
    skill_manager = SkillManager(launch_directory / "skills")
    skill_manager.ensure_directory()
    agent = AriaAgent(
        provider,
        registry,
        context,
        memory,
        max_iterations=config.max_iterations,
        persona=config.persona,
        skill_manager=skill_manager,
        profile_path=config.background.profile_path,
    )
    agent.tool_attachments = pending_image_attachments
    agent.image_fallback_provider = vision_fallback_provider

    repl_kwargs: dict[str, Any] = dict(
        provider_manager=provider_manager,
        config=config,
        speech=speech,
        coder_service=coder_service,
        deploy_handler=deploy_handler,
        web_service=web_service,
        skill_manager=skill_manager,
        vision_config=config.vision,
        image_fallback_provider=vision_fallback_provider,
        scheduler_service=scheduler_service,
        proactive_service=proactive_service,
    )
    try:
        repl = create_repl(agent, console, **repl_kwargs)
        try:
            repl.run()
        except Exception as exc:
            # BR-3: an urwid thread crash used to look like a clean exit.
            # Propagate it into a controlled Rich fallback when possible.
            from .ui.urwid_tui import UrwidRepl  # lazy: keeps urwid optional

            if not isinstance(repl, UrwidRepl):
                raise
            log_error(f"Startup: urwid TUI crashed: {type(exc).__name__}: {exc}")
            console.print(
                f"The urwid interface crashed: {type(exc).__name__}: {exc}\n"
                "Falling back to the Rich interface for this session.",
                style="yellow",
            )
            from .ui.repl import Repl

            Repl(agent, console, **repl_kwargs).run()
    finally:
        if scheduler_service is not None:
            scheduler_service.stop()
        memory.cleanup()
        if browser_service is not None:
            browser_service.close()
    log_info("ARIA shut down cleanly")
    return 0


def _validate_model_choice(manager: Any, config: AppConfig) -> str | None:
    """Return a replacement model when the configured one is unavailable.

    Checks the provider's static model list first, then the live inventory
    (Ollama's local list or the endpoint's /models). Returns None when the
    configured model is usable or cannot be verified (e.g. the server is
    down) — in that case the provider surfaces a clear error per turn.
    """
    provider = config.provider
    model = config.model
    state_changed_model = bool({"model", "provider"} & set(config.runtime_state_overrides))
    try:
        static_models = manager.models_for(provider)
        if model in static_models and not state_changed_model:
            return None
        live = list_models(manager.create(provider, model))
    except (ValueError, OSError) as exc:
        log_info(f"Startup: could not validate model {model!r} on {provider}: {exc}")
        return None
    if not live:
        log_info(f"Startup: could not verify model {model!r} on {provider}; continuing")
        return None
    if model in live:
        return None
    fallback = next((candidate for candidate in static_models if candidate in live), live[0])
    log_error(f"Startup: model {model!r} is not available on {provider}; falling back to {fallback!r}")
    return fallback


def _ensure_web_backend(launch_directory: Path, web: WebConfig) -> bool:
    """Best-effort check that the local SearXNG backend is reachable.

    Non-fatal by design (bugReport BR-1): a failed check only disables web
    tools for this session. If a local docker compose project for SearXNG
    exists AND ``web.start_backend`` is enabled, ARIA tries to start it and
    polls the JSON search endpoint for up to 30 seconds.
    """
    if _is_searxng_reachable(web.searxng_url):
        return True

    if not web.start_backend:
        log_info("Startup: SearXNG not reachable; web.start_backend is false, continuing without it")
        return False

    compose_path = launch_directory / "infrastructure" / "searxng" / "docker-compose.yml"
    if compose_path.is_file() and shutil.which("docker"):
        log_info("Startup: SearXNG not reachable; attempting docker compose up -d")
        try:
            subprocess.run(
                ["docker", "compose", "-f", str(compose_path), "up", "-d"],
                check=True,
                cwd=str(launch_directory),
            )
        except subprocess.CalledProcessError as exc:
            log_error(f"Startup: docker compose failed: {exc}")
        except OSError as exc:
            log_error(f"Startup: docker compose unavailable: {exc}")

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _is_searxng_reachable(web.searxng_url):
            log_info("Startup: SearXNG backend is now reachable")
            return True
        time.sleep(1.0)
    return False


def _is_searxng_reachable(searxng_url: str) -> bool:
    try:
        import httpx

        with httpx.Client(timeout=2.0, follow_redirects=True) as client:
            response = client.get(f"{searxng_url}/search", params={"q": "aria", "format": "json"})
        return response.status_code == 200 and isinstance(response.json(), dict)
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
