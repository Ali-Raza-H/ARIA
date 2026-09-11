# Multimodal input and speech

ARIA treats images and speech as optional extensions of the core text agent. The core conversation remains usable when these dependencies are absent.

## Image input

Images can be queued for the next message through:

```text
/attach clipboard
/attach /path/to/image.png
/screen
```

The image is normally ephemeral and is attached only to the first provider request of the turn.

## Clipboard images

`/attach clipboard` uses the configured clipboard backend. On Wayland/X11 systems ARIA can use the available helper such as `wl-paste` or `xclip`, with the Python path available according to configuration.

A normal terminal Ctrl+V operation does not magically transfer binary image data into a text prompt. ARIA therefore exposes an explicit image-attachment path rather than pretending a binary paste succeeded.

## File images

`/attach /path/to/image.png` loads a supported image file. Image size is bounded by `vision.max_image_bytes`.

Keep large source images out of unnecessary model requests because multimodal models can have substantial image-processing cost.

## Screenshots

`/screen` captures a temporary screenshot using the configured screenshot command.

Periodic screen analysis is different. It requires explicit scheduler/vision settings and is not enabled simply because interactive screenshots are available.

Screens can contain sensitive information such as passwords, private messages, financial information, or credentials. Keep periodic screen context disabled unless there is a concrete use case.

## Native vision vs fallback

When the active model supports image input, ARIA sends an image part through the provider's native multimodal interface.

When the active model is text-only, ARIA can use a configured visual fallback provider/model:

```yaml
vision:
  enabled: true
  fallback_provider: <provider>
  fallback_model: <visual-model>
```

The fallback exists so the main conversational model does not need to be replaced merely because a single turn contains an image.

## Periodic screen context

Periodic screen analysis is represented as a scheduler job and uses the same durable scheduling/audit path as other autonomous analysis.

It requires the appropriate `vision.periodic_screen_enabled` setting and scheduler/autonomy configuration. Screenshots are temporary and are not retained by default.

## Speech architecture

Speech is handled by `SpeechController`. Engines are loaded lazily and can disable themselves after a dependency or audio-device failure.

Supported engines:

- `kokoro_hf` — hosted Kokoro through Hugging Face.
- `kokoro_local` — local Kokoro `KPipeline`.
- `chatterbox` — local Chatterbox TTS/custom voice support.

The legacy `kokoro` engine name aliases to hosted Kokoro.

## Installing speech engines

Hosted Kokoro:

```bash
uv sync --extra voice
```

Local Kokoro:

```bash
uv sync --extra voice-local
```

Local Chatterbox:

```bash
uv sync --extra voice-chatterbox
```

For local Kokoro, the host also needs `espeak-ng`. Audio playback requires an appropriate PortAudio/PipeWire setup on Linux.

## Markdown-aware speech

ARIA does not send raw Markdown syntax directly to the speech engine. It strips constructs such as emphasis markers, links, fences, and table formatting and converts machine-oriented values into speech-friendly wording.

For example, an ISO timestamp can be spoken as a natural date/time rather than as a punctuation-heavy machine string.

This preprocessing makes speech understandable without changing the text displayed in the TUI.

## Runtime commands

```text
/tts on
/tts off
/tts kokoro_hf
/tts kokoro_local
/tts chatterbox
/tts status
```

Use `/tts status` when diagnosing dependency/audio problems.

## Data handling

Images are ephemeral by default. Do not assume that attaching an image makes it part of durable memory. If a model extracts a durable fact from an image, that fact enters the normal memory pipeline and follows the memory subsystem's promotion/retention rules.
