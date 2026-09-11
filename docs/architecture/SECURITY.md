# Security and trust boundaries

ARIA is a local personal assistant with capabilities that can affect files, processes, websites, desktop state, external services, and personal data. Its security model is therefore primarily about **capability boundaries, credential isolation, bounded execution, and explicit configuration**.

## Threat model

ARIA should be treated as a trusted local automation process, not as an unprivileged text chatbot. A model can misunderstand a request, produce malformed tool arguments, or attempt an unnecessary action. The implementation therefore validates tool arguments and applies service/configuration boundaries instead of assuming model intent is always safe.

At the same time, some capabilities are intentionally powerful. If you enable unrestricted desktop control, persistent browser credentials, Gmail writes, Docker operations, or autonomous computer control, you are granting the assistant meaningful local authority.

## Credential boundary

Secrets belong outside source-controlled YAML:

```text
.env                         hosted API keys and environment overrides
local OAuth/token files      Gmail credentials when configured
config.yaml                  non-secret static configuration
```

Provider definitions name environment variables through `aria_api_key_env` and `coder_api_key_env`; the actual secret value is not stored in the provider block.

The ARIA and coder credentials are intentionally separate. A coder request must not silently inherit ARIA's hosted-provider key.

## Runtime-state boundary

Interactive preferences are written to:

```text
data/state/aria-state.yaml
```

This file is ignored by Git and contains selected runtime preferences such as provider/model, workspace, iteration limits, speech state, coder iterations, and trace settings.

Security-sensitive configuration and credentials are not intended to become runtime-state overrides. Use `--ignore-state` or `--reset-state` when debugging stale runtime selections.

## Shell and filesystem

ARIA exposes filesystem and shell capabilities through registered tools. The shell path is not the same thing as a general-purpose GUI launcher.

Configured desktop launchers are blocked from being invoked as arbitrary shell commands so the model uses the named `desktop_launch` route instead. Launcher values are argv arrays rather than shell strings, reducing quoting/injection ambiguity.

The configured `workspace` defines the normal filesystem/shell operating root. Do not run ARIA with a workspace containing secrets you do not want the assistant to access.

## Browser security

The Playwright browser is persistent when enabled. Cookies and login state can therefore survive restarts.

**Never point `browser.profile_directory` at a profile currently used by your everyday browser.** Use a dedicated agent profile.

Browser actions can include navigation, clicks, typing, downloads, and uploads. Treat the browser as a real authenticated user session, not as a harmless scraper.

The browser automation route is independent from a native desktop browser launcher. Enabling one does not implicitly enable the other.

## Web fetching and SSRF protection

ARIA's webpage tool blocks private/local destinations by default, including loopback, link-local, and RFC1918-style internal networks. `web.allowed_hosts` can explicitly permit internal hostnames when the operator intentionally needs them.

SearXNG is normally local and does not require a search API key. Search and page-fetch counts, sizes, concurrency, retries, and cache durations are bounded by configuration.

When adding new web-fetch behavior, preserve the destination validation before network access. Do not allow the model to bypass it by changing URL parsing or redirect handling without an explicit security review.

## Desktop security

Desktop control has two modes:

### Managed

Managed mode provides validated inspection and allow-listed actions, named application launchers, input, and screenshots.

### Unrestricted

Unrestricted mode additionally enables raw Hyprland dispatch, marked keybind editing, configured desktop shell commands, and arbitrary desktop shell capabilities.

Unrestricted mode should be considered **full trusted-local-agent control**. The current implementation intentionally does not prompt before every click, keystroke, download, upload, app launch/close, or window change. Do not enable it casually.

## Gmail

Gmail has two integration paths:

1. Google's official MCP endpoint for supported operations.
2. A separately enabled direct REST fallback for operations not covered by the MCP path.

The direct fallback can expose highly consequential actions such as send, reply, forward, trash, permanent deletion, labels, settings, contacts, and attachment downloads. Confirmation behavior is implemented around consequential operations, subject to the current request explicitly naming the action and target.

OAuth files should have restrictive OS permissions and should never be committed.

## System and Docker

System operations can expose metrics, systemd state, journal logs, and other host information. Docker operations use the Docker CLI rather than directly mounting the Docker socket into ARIA.

Docker access is still powerful because a user with Docker privileges may effectively control the host. Keep the process's OS-level permissions narrow and enable the feature only when needed.

## Autonomous actions

Scheduler analysis and consequential autonomy are deliberately separated.

Read-only analysis requires `scheduler.analysis_enabled`. Consequential categories additionally require `autonomy.enabled` and the category to appear in `autonomy.allowed_categories`.

Supported categories include:

- `analysis`
- `lifeos_writes`
- `notifications`
- `computer_control`
- `timers`

LifeOS writes have another allowlist: `autonomy.lifeos_write_operations`.

Every scheduled attempt, including blocked and failed actions, is persisted in scheduler state for auditing.

## Screen context

Normal image attachments and `/screen` are ephemeral inputs. Periodic screen analysis is separately opt-in and requires the periodic screen setting in addition to scheduler/autonomy configuration.

Screenshots can contain passwords, private messages, financial information, or other sensitive data. Keep periodic screen analysis disabled unless you have a concrete reason to use it.

## Logging and telemetry

ARIA writes runtime logs only to `data/logs/aria.log` and redacts secrets before writing.

Telemetry is deliberately metadata-oriented. It records operational measurements such as provider/model, message counts, context size, token estimates, latency, tool counts, iteration counts, and error metadata, but does not intentionally store prompt, response, tool-argument, or tool-result contents. Argument hashes may be retained for correlation.

Never add raw credentials or full personal-content payloads to logs while debugging.

## Safe configuration baseline

A conservative starting point is:

```yaml
browser:
  enabled: false

desktop:
  enabled: false
  mode: managed

gmail:
  enabled: false

system:
  enabled: false

docker:
  enabled: false

scheduler:
  enabled: false

autonomy:
  enabled: false
  allowed_categories: []

vision:
  periodic_screen_enabled: false
```

Enable capabilities one at a time and verify their behavior before adding another trust boundary.

## Incident/debugging checklist

If ARIA performs an unexpected action:

1. Stop ARIA.
2. Disable the relevant feature in `config.yaml`.
3. If autonomous behavior is involved, disable `autonomy.enabled` and scheduler analysis.
4. Inspect `data/logs/aria.log` without exposing it publicly.
5. Review scheduler audit records if the action was scheduled.
6. Review browser profile activity if the browser was involved.
7. Rotate affected API credentials if a credential may have been exposed.
8. Reproduce in a minimal configuration before restoring capabilities.

Do not publish `.env`, OAuth tokens, browser profiles, runtime databases, or logs in an issue or pull request.
