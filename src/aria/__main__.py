"""ARIA command-line entrypoint."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from rich.console import Console

from .agent.aria import AriaAgent, register_deploy_coder_tool
from .agent.coder import CoderService
from .config import ConfigError, load_config
from .llm.factory import ProviderManager
from .logging_setup import configure_logging, log_error, log_info
from .persistent_memory import MemoryError, PersistentMemory
from .memory import SessionMemory
from .speech import SpeechController
from .tools import (
    ToolContext,
    ToolRegistry,
    register_filesystem_tools,
    register_lifeos_tool,
    register_shell_tool,
    WebToolService,
    register_web_tools,
)
from .tools.web import SearXNGProvider
from .ui.repl import Repl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ARIA - Adaptive Reasoning and Intelligence Assistant")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to the YAML configuration file (default: config.yaml)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    console = Console()
    launch_directory = Path.cwd()

    try:
        config = load_config(args.config, launch_directory)
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
        provider = provider_manager.create(config.provider, config.model)
        provider_manager.set_active_model(config.provider, config.model)
    except (ValueError, OSError) as exc:
        log_error(f"Startup: provider initialization failed: {exc}")
        console.print(f"Provider error: {exc}", style="red")
        return 1

    # ARIA's own toolset: she converses and delegates; heavy tools belong to the coder.
    # ARIA's toolset: she converses, runs quick commands herself, and
    # delegates heavy work to the independent coder agent.
    registry = ToolRegistry()
    register_filesystem_tools(registry)
    register_shell_tool(registry)
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

    if config.memory.enabled:
        try:
            memory = PersistentMemory(launch_directory, config.memory, provider)
        except MemoryError as exc:
            log_error(f"Startup: persistent memory initialization failed: {exc}")
            console.print(f"Memory error: {exc}", style="red")
            return 1
    else:
        memory = SessionMemory(launch_directory / "data" / "sessions", label="aria")
    context = ToolContext(
        workspace=config.workspace,
        command_timeout_seconds=config.command_timeout_seconds,
        max_command_output_chars=config.max_command_output_chars,
    )
    agent = AriaAgent(
        provider,
        registry,
        context,
        memory,
        max_iterations=config.max_iterations,
        persona=config.persona,
    )
    speech = SpeechController(config.speech)

    try:
        Repl(
            agent,
            console,
            provider_manager=provider_manager,
            config=config,
            speech=speech,
            coder_service=coder_service,
            deploy_handler=deploy_handler,
            web_service=web_service,
        ).run()
    finally:
        memory.cleanup()
    log_info("ARIA shut down cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
