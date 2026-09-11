# ARIA Documentation

Welcome to the ARIA documentation. ARIA (Adaptive Reasoning and Intelligence Assistant) is a Python 3.11 terminal personal assistant with an independent coding agent, persistent hybrid memory, local web research, optional browser and Hyprland desktop control, scheduling/autonomy, multimodal input, speech, LifeOS integration, Gmail/system/media integrations, and extensible skills/workflows.

This documentation is intentionally implementation-oriented. It describes how the current repository is structured, how runtime state flows through the application, how features are enabled, what data each subsystem owns, and how to develop new capabilities without bypassing ARIA's existing boundaries.

## Documentation map

### Getting started

- [Installation](getting-started/INSTALLATION.md) — prerequisites, Python environment, dependencies, optional system packages, first launch, and verification.
- [Quick start](getting-started/QUICKSTART.md) — get from a fresh clone to a working conversation and progressively enable features.
- [Commands](getting-started/COMMANDS.md) — interactive TUI commands and operational shortcuts.

### Concepts and architecture

- [Architecture](architecture/ARCHITECTURE.md) — process topology, startup sequence, conversation/tool loop, service boundaries, and data flow.
- [Agent loop](architecture/AGENT_LOOP.md) — how ARIA constructs context, calls models, executes tools, handles iterations, and delegates heavy work.
- [Memory architecture](architecture/MEMORY.md) — SQLite hard facts plus Chroma semantic/history retrieval, extraction, ranking, degradation, and retention.
- [Security model](architecture/SECURITY.md) — trust boundaries, credentials, shell/browser/desktop capabilities, SSRF protections, confirmations, and autonomous actions.

### Configuration

- [Configuration reference](configuration/CONFIGURATION.md) — every major configuration section and precedence rules.
- [Environment and secrets](configuration/ENVIRONMENT.md) — `.env`, provider credentials, OAuth material, and safe configuration practices.

### Features

- [Coding agent](features/CODING_AGENT.md) — independent coder architecture, deployment lifecycle, budgets, provider selection, and workspace behavior.
- [Web research](features/WEB_RESEARCH.md) — local SearXNG, search/open-page tools, caching, limits, retries, and SSRF boundaries.
- [Browser automation](features/BROWSER.md) — Playwright persistent browser context, profiles, navigation, interaction, and safety considerations.
- [Desktop control](features/DESKTOP.md) — Hyprland managed/unrestricted modes, launchers, input backends, screenshots, and permissions.
- [Scheduling and autonomy](features/SCHEDULER.md) — cron workflows, timers, briefings, audit records, notifications, and autonomy categories.
- [Multimodal and speech](features/MULTIMODAL.md) — image attachments, screen capture, visual fallback, Kokoro, Chatterbox, and Markdown-aware speech.
- [LifeOS](features/LIFEOS.md) — optional connection to the LifeOS API and proactive personal-management workflows.
- [Gmail, media, system, and Docker](integrations/GMAIL_SYSTEM_MEDIA.md) — external integrations, operation classes, credentials, and confirmation behavior.
- [Skills and workflows](features/SKILLS_WORKFLOWS.md) — extending behavior through Markdown skills and scheduled YAML workflows.

### Development

- [Development guide](development/DEVELOPMENT.md) — development environment, repository map, extension points, tests, and type checking.
- [Telemetry](development/TELEMETRY.md) — privacy-conscious operational metrics and retention.
- [Troubleshooting](development/TROUBLESHOOTING.md) — common startup, provider, memory, web, browser, desktop, speech, and scheduler failures.

## Source-of-truth rules

The repository contains several kinds of information. Use the following order when resolving discrepancies:

1. The current implementation under `src/aria/` defines actual runtime behavior.
2. `config.example.yaml` defines the supported configuration surface and safe defaults.
3. `pyproject.toml` defines Python/package requirements and optional extras.
4. Maintained documentation explains the implementation and intended operator workflow.
5. `docs/reports/` contains historical engineering reports and is not the canonical user/developer reference.

If documentation and code disagree, update the documentation after verifying the implementation rather than silently documenting the old behavior.

## Runtime data layout

ARIA deliberately separates source-controlled configuration from generated state:

```text
data/
├── logs/                         # the single runtime log location
│   └── aria.log
├── memory/                       # SQLite facts + Chroma collections
├── scheduler/                    # scheduler SQLite state and audit data
├── sessions/                     # session-only memory/fallback data
├── state/                        # ignored runtime UI preferences
│   └── aria-state.yaml
├── browser-profile/              # persistent Playwright browser profile
└── gmail/                        # local OAuth material when configured
```

Do not commit secrets, browser profiles, runtime databases, generated logs, or machine-specific state.

## Design principles

ARIA's implementation is organized around a few important principles:

- **Local-first:** optional services should fail independently and degrade rather than making the core chat unusable.
- **Provider-neutral:** hosted providers are normalized behind a common interface; Ollama can use a portable text-based tool-call protocol.
- **Separate contexts:** ARIA's conversational context is kept separate from the independent coding agent's large task context.
- **Explicit capabilities:** browser, desktop, Gmail, system, Docker, web, speech, vision, scheduler, and LifeOS features are separately configured.
- **Bounded execution:** model iterations, command output, web calls, page size, retries, and other resource-heavy operations have explicit limits.
- **Auditable autonomy:** scheduled/consequential actions are persisted and categorized rather than treated as invisible background magic.
- **Secrets stay outside source:** API keys and OAuth tokens belong in environment variables or local credential files, never YAML committed to Git.

## Contributing documentation

When adding or changing a feature, update the documentation at the same time. A useful feature document should answer five questions:

1. What does the feature do?
2. How is it enabled?
3. What dependencies and credentials are required?
4. What can it access or change?
5. What happens when the dependency is unavailable?

For architecture changes, update the relevant architecture page and explain new data/control boundaries. For new tools, document their schema, confirmation requirements, failure modes, and tests.
