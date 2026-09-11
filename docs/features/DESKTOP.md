# Hyprland desktop control

ARIA can optionally operate a Hyprland/Wayland desktop through a dedicated desktop tool service. This feature is intentionally separate from the shell tool and from browser automation.

## Enablement

Recommended starting configuration:

```yaml
desktop:
  enabled: true
  mode: managed
  input_backend: arch
  hyprctl_command: hyprctl
  wtype_command: wtype
  ydotool_command: ydotool
  screenshot_command: grim
  command_timeout_seconds: 15
  launchers:
    terminal: [kitty]
    browser: [firefox]
```

On Arch Linux, the example stack uses:

```bash
sudo pacman -S hyprland wtype ydotool grim slurp
```

## Managed mode

`managed` is the recommended mode. It exposes validated/allow-listed desktop operations such as:

- monitor inspection;
- window inspection;
- workspace inspection;
- approved Hyprland dispatchers;
- named application launchers;
- keyboard/mouse input;
- screenshots.

Native applications should be launched through configured `desktop.launchers` routes rather than through arbitrary shell commands.

Example:

```yaml
launchers:
  terminal: [kitty]
  browser: [firefox]
  editor: [code]
```

The shell tool blocks the executable names used by these configured launcher routes so the model is directed toward `desktop_launch` instead.

## Unrestricted mode

```yaml
mode: unrestricted
```

adds significantly more authority:

- raw `hyprctl dispatch`;
- marked keybind editing through `desktop_set_keybind`;
- configured desktop commands;
- arbitrary desktop shell commands.

Treat unrestricted mode as **full trusted-local-agent desktop control**. It is not merely a debugging mode.

## Input backends

### Arch/Wayland backend

The default `arch` backend uses configured utilities such as `wtype` and `ydotool` for keyboard/input operations and `grim` for screenshots.

These utilities interact with the active Wayland session and may require session-specific permissions/configuration.

### PyAutoGUI fallback

Install:

```bash
uv sync --extra desktop
```

Then configure:

```yaml
input_backend: pyautogui
```

PyAutoGUI has its own Wayland limitations and is not a universal replacement for native Wayland tools.

## Keyboard input

Keyboard text is sent through the configured input backend. Chords use one input invocation with modifier holds/releases so modifiers are not released before the target key is delivered.

When extending this system, prefer the structured desktop input tool over constructing ad-hoc shell commands for key sequences.

## Screenshots

The desktop service can capture screenshots using the configured screenshot command. Screenshots are image data and can be passed through ARIA's multimodal path when vision is configured.

Do not enable periodic screen analysis merely because screenshots work interactively; periodic analysis is a separate scheduler/vision setting.

## Native app launching

Launchers are argv arrays, not shell strings:

```yaml
launchers:
  terminal: [kitty]
  browser: [firefox, --new-window]
```

This avoids needing shell parsing for application launch and makes the allowed route explicit.

Do not configure a launcher to run a shell interpreter unless you intentionally want to expose that capability.

## Security model

Desktop control can affect the entire local session. Depending on configuration, ARIA may move windows, switch workspaces, send input, launch/close applications, download/upload through other tools, and execute commands.

The current implementation does not ask for confirmation before every click, typing action, download, upload, input event, window change, or application launch/close. Operators must therefore treat the enabled desktop capability as trusted automation.

Keep managed mode enabled unless the additional unrestricted operations are genuinely required.

## Troubleshooting

Check utilities:

```bash
which hyprctl
which wtype
which ydotool
which grim
```

Check that ARIA is running inside the expected Wayland/Hyprland session. If `wtype` or `ydotool` fail, investigate the session permissions before switching backends.

If a launcher does not work, verify that the executable exists and that the launcher is an argv list rather than a shell string.
