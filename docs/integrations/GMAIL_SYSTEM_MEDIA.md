# Gmail, media, system, and Docker integrations

## Gmail

ARIA integrates with Google's official Gmail remote MCP endpoint:

```text
https://gmailmcp.googleapis.com/mcp/v1
```

Google currently documents this service as Developer Preview. It provides `search_threads`, `get_message`, `get_thread`, `create_draft`, `list_drafts`, `list_labels`, `label_message`, `label_thread`, `unlabel_message`, and `unlabel_thread`. Google documents the MCP OAuth scopes as `gmail.readonly` and `gmail.compose`; the direct fallback requires broader Google API scopes and is separately configured.

Enable Gmail API and Gmail MCP API in Google Cloud, configure OAuth, and provide the resulting access token through `GMAIL_MCP_ACCESS_TOKEN`. Google documents the Gmail scopes and setup at [Configure the Gmail MCP server](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server).

ARIA also has a direct Gmail/People REST fallback for actions not currently exposed by Google's MCP preview: sending, replying, forwarding, trashing, permanent deletion, message modification, label creation/deletion, filters, settings, contacts, and attachment downloads. Enable it with `gmail.direct_api_enabled: true` and provide an OAuth access token with the required broader scopes as `GMAIL_API_ACCESS_TOKEN`, or place an existing OAuth token JSON at `gmail.oauth_token_path`. The direct fallback is deliberately separate from the official MCP token.

### Confirmation rule

- Read operations and draft creation do not require confirmation.
- Destructive or consequential actions require confirmation unless the current user request clearly expresses the action and target in conversational language. Examples: “restart nginx”, “stop the API service for maintenance”, “send this email to Alex”, or “delete that message permanently”.
- If the request is ambiguous or the model inferred the action, ARIA returns `CONFIRMATION_REQUIRED` and asks for confirmation before repeating the exact call.
- Credentials are never included in model results or logs.

Email bodies and external content are untrusted data. ARIA must not follow instructions found inside an email.

## Media

`media_status` reports active players through `playerctl`. `media_control` supports play, pause, play-pause, stop, next, previous, seek, volume, and mute. `media_launch` uses named executable routes configured under `media.players`, for example:

```yaml
media:
  enabled: true
  players:
    yt: [mpv, https://www.youtube.com]
    spotify: [spotify]
    termusic: [termusic]
```

Add any local player as an argv array. Launch routes do not use a shell.

## System monitoring and systemd

`system_monitor` provides overview, CPU, memory, disk, temperatures, GPU, battery, network, and process metrics using configured host utilities. Missing utilities produce a bounded tool error rather than crashing ARIA.

`systemctl_control` supports read-only status and unit listing, bounded journal logs, and service start/stop/restart/reload/enable/disable/mask/unmask. Service changes require confirmation unless the user directly or conversationally asked for the action and named the target. Journal output is limited by the requested line count.

## Docker

`docker_control` uses the configured Docker CLI, not the Docker socket/API. It supports:

- Read-only: `ps`, `images`, `volumes`, `networks`, `stats`, `inspect`, `logs`, `top`, `events`, `version`, `info`, and `compose-ps`.
- Operations: start, stop, restart, pause, unpause, kill, remove containers/images/volumes/networks, pull, container exec, Compose up/down/restart, and system prune.

All non-read-only Docker operations require confirmation unless the user clearly requested the operation and target. Use a narrow `docker_command` path and avoid granting unrestricted shell access when this toolset is sufficient.

## Configuration

The fields are present in both `config.example.yaml` and `config.yaml` under `gmail`, `media`, `system`, and `docker`. Behavior constants and supported action lists are also available in `src/aria/config/behaviour.py`.
