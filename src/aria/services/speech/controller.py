"""Text-to-speech backends for hosted/local Kokoro and local Chatterbox.

All optional speech engines are imported lazily. Speech is disabled unless
config.yaml (or ARIA_SPEECH_ENABLED=true) turns it on.
"""

from __future__ import annotations

import io
import os
import re
import sys
import warnings
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime
from typing import Any

from ...config import SpeechConfig
from ...logging.setup import log_debug, log_error, log_info

# --- Markdown stripping ----------------------------------------------------
#
# LLM replies arrive as Markdown and TTS engines should read the words, not
# the syntax. These patterns cover the constructs the assistant actually
# emits: headings, emphasis, lists, quotes, links, tables, horizontal rules,
# and the fences around code blocks (the code itself is kept).

_FENCED_CODE_RE = re.compile(r"(?ms)^(?:```|~~~).*?(?:```|~~~|\Z)")
_FENCE_LINE_RE = re.compile(r"^\s*(?:```|~~~)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC_STAR_RE = re.compile(r"\*(?=\S)([^*]+?)(?<=\S)\*")
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<![\w*])_(?=\S)([^_]+?)_(?![\w*])")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")
_HEADING_RE = re.compile(r"^#{1,6}\s+")
_QUOTE_RE = re.compile(r"^\s*>+\s?")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+")
_HRULE_RE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_TIME_RE = re.compile(r"(?<![\w:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*([APap][Mm]))?")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def conversationalize_for_speech(text: str) -> str:
    """Turn machine-formatted dates/times into phrases speech engines say naturally."""
    def replace_time(match: re.Match[str]) -> str:
        hour = int(match.group(1))
        minute = int(match.group(2))
        second = match.group(3)
        suffix = match.group(4)
        if suffix:
            meridiem = suffix.upper()
            hour = hour % 12 or 12
        else:
            meridiem = "AM" if hour < 12 else "PM"
            hour = hour % 12 or 12
        value = f"{hour}:{minute:02d} {meridiem}"
        if second is not None:
            value = f"{hour}:{minute:02d}:{int(second):02d} {meridiem}"
        return value

    def replace_date(match: re.Match[str]) -> str:
        try:
            return datetime.strptime(match.group(0), "%Y-%m-%d").strftime("%B %-d, %Y")
        except ValueError:
            return match.group(0)

    text = _ISO_DATE_RE.sub(replace_date, text)
    return _TIME_RE.sub(replace_time, text)


_TORCH_NOISE_RE = re.compile(
    r"(?i)(torch|torchaudio|cuda|cudnn|tensorflow|triton|mps|userwarning|futurewarning|"
    r"not compiled|not available|falling back)"
)


@contextmanager
def _quiet_torch_output():
    """Hide noisy torch/Kokoro startup warnings while preserving exceptions.

    Some local Kokoro/torch builds print non-actionable backend notices directly
    to stdout/stderr instead of using Python logging. Capture only while the
    optional backend is loading or synthesizing; unexpected non-torch output is
    retained in the file log at DEBUG rather than shown over the user's prompt.
    """
    stdout = io.StringIO()
    stderr = io.StringIO()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, module=r"(?i)(torch|torchaudio|kokoro)")
        warnings.filterwarnings("ignore", category=FutureWarning, module=r"(?i)(torch|torchaudio|kokoro)")
        warnings.filterwarnings("ignore", message=r"(?i).*\\b(torch|cuda|cudnn|torchaudio)\\b.*")
        with redirect_stdout(stdout), redirect_stderr(stderr):
            yield
    for captured in (stdout.getvalue(), stderr.getvalue()):
        for line in captured.splitlines():
            if line.strip() and not _TORCH_NOISE_RE.search(line):
                log_debug(f"Speech backend output: {line[:500]}")


def strip_markdown_for_speech(text: str) -> str:
    """Return *text* with Markdown syntax removed, ready for text-to-speech."""

    def _unfence(match: re.Match[str]) -> str:
        return "\n".join(
            line for line in match.group(0).splitlines() if not _FENCE_LINE_RE.match(line)
        )

    text = _FENCED_CODE_RE.sub(_unfence, text)

    cleaned: list[str] = []
    for line in text.splitlines():
        if _HRULE_RE.match(line) or _TABLE_SEPARATOR_RE.match(line):
            continue
        line = _HEADING_RE.sub("", line)
        line = _QUOTE_RE.sub("", line)
        line = _BULLET_RE.sub("", line)
        cleaned.append(line)
    text = "\n".join(cleaned)

    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _IMAGE_RE.sub(r"\1", text)  # images: keep the alt text only
    text = _LINK_RE.sub(r"\1", text)  # links: keep the label, drop the URL
    text = _AUTOLINK_RE.sub(r"\1", text)
    text = _BOLD_RE.sub(r"\2", text)
    text = _ITALIC_STAR_RE.sub(r"\1", text)
    text = _ITALIC_UNDERSCORE_RE.sub(r"\1", text)
    text = text.replace("|", " ")  # remaining table pipes become pauses
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class KokoroHFBackend:
    """Kokoro-82M via Hugging Face-hosted inference."""

    name = "kokoro_hf"

    def __init__(self, config: SpeechConfig) -> None:
        self._config = config
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from huggingface_hub import InferenceClient

            api_key = None
            env_name = self._config.hf_api_key_env
            if env_name:
                api_key = os.getenv(env_name)
            if not api_key:
                raise RuntimeError(
                    f"Hugging Face speech needs a token: set {env_name} in .env "
                    "(https://huggingface.co/settings/tokens)"
                )
            log_info(
                f"Speech: using hosted Kokoro model={self._config.hf_model} "
                f"provider={self._config.hf_provider}"
            )
            provider_arg = self._config.hf_provider
            if provider_arg in {"", "auto", None}:
                # Let Hugging Face route to any available inference provider.
                self._client = InferenceClient(api_key=api_key)
            else:
                self._client = InferenceClient(provider=provider_arg, api_key=api_key)  # type: ignore[arg-type]
        return self._client

    def speak(self, text: str) -> None:
        import sounddevice as sd
        import soundfile as sf

        client = self._ensure_client()
        log_debug(f"Speech: requesting hosted audio for {len(text)} chars")
        audio_bytes = client.text_to_speech(
            text,
            model=self._config.hf_model,
        )
        audio, samplerate = sf.read(io.BytesIO(audio_bytes), dtype="float32")
        sd.play(audio, samplerate=samplerate)
        sd.wait()


class KokoroLocalBackend:
    """Kokoro-82M running locally through its KPipeline interface."""

    name = "kokoro_local"

    def __init__(self, config: SpeechConfig) -> None:
        self._config = config
        self._pipeline = None

    def _ensure_pipeline(self):
        if self._pipeline is None:
            with _quiet_torch_output():
                from kokoro import KPipeline

                log_info(f"Speech: loading local Kokoro model lang_code={self._config.lang_code}")
                self._pipeline = KPipeline(lang_code=self._config.lang_code)
        return self._pipeline

    def speak(self, text: str) -> None:
        import sounddevice as sd

        pipeline = self._ensure_pipeline()
        try:
            device = sd.query_devices(kind="output")
            log_info(
                f"Speech: local Kokoro output device={device.get('name', 'unknown')} "
                f"channels={device.get('max_output_channels', 0)}"
            )
            sd.check_output_settings(samplerate=24_000, channels=1)
        except Exception as exc:
            raise RuntimeError(f"No usable audio output at 24 kHz: {exc}") from exc
        log_debug(f"Speech: synthesizing local Kokoro audio for {len(text)} chars")
        chunks = 0
        with _quiet_torch_output():
            generator = pipeline(
                text,
                voice=self._config.voice,
                speed=self._config.speed,
                split_pattern=r"\n+",
            )
            audio_chunks = list(generator)
        for _, _, audio in audio_chunks:
            # Kokoro's optional dependency stubs expose several possible
            # tensor/array types; normalize through Any after runtime checks.
            audio_value: Any = audio
            if hasattr(audio_value, "detach"):
                audio_value = audio_value.detach().cpu().numpy()
            elif hasattr(audio_value, "cpu"):
                audio_value = audio_value.cpu().numpy()
            elif hasattr(audio_value, "numpy"):
                audio_value = audio_value.numpy()
            if hasattr(audio_value, "squeeze"):
                audio_value = audio_value.squeeze()
            if hasattr(audio_value, "astype"):
                audio_value = audio_value.astype("float32", copy=False)
            if not hasattr(audio_value, "size") or audio_value.size == 0:
                continue
            sd.play(audio_value, samplerate=24_000, blocking=True)
            sd.wait()
            chunks += 1
        if chunks == 0:
            raise RuntimeError("Local Kokoro produced no audio chunks")


# Backward-compatible name for callers that used the original hosted backend.
KokoroBackend = KokoroHFBackend


class ChatterboxBackend:
    """Chatterbox TTS backend with custom voice cloning support (local)."""

    name = "chatterbox"

    def __init__(self, config: SpeechConfig) -> None:
        self._config = config
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from chatterbox.tts import ChatterboxTTS

            log_info("Speech: loading Chatterbox model (first use)")
            self._model = ChatterboxTTS.from_pretrained(device="cpu")
        return self._model

    def speak(self, text: str) -> None:
        import tempfile

        import sounddevice as sd
        import soundfile as sf

        model = self._ensure_model()
        kwargs = {}
        if getattr(self._config, "voice", None) and self._config.voice not in {"af_sarah", "default"}:
            # Chatterbox accepts an audio sample path for custom voice cloning.
            kwargs["audio_prompt_path"] = self._config.voice
        wav = model.generate(text, **kwargs)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            sf.write(handle.name, wav.squeeze(0).numpy(), model.sr)
            audio, samplerate = sf.read(handle.name)
        sd.play(audio, samplerate=samplerate)
        sd.wait()


class SpeechController:
    """Routes speech requests to the configured engine; safe when disabled."""

    def __init__(self, config: SpeechConfig) -> None:
        self._config = self._canonical_config(config)
        self._backend = None
        self._unavailable = False
        self._last_error: str | None = None
        log_debug(
            f"Speech: controller created engine={self._config.engine} "
            f"enabled={self._config.enabled}"
        )

    @staticmethod
    def _canonical_config(config: SpeechConfig) -> SpeechConfig:
        if config.engine == "kokoro":
            return replace(config, engine="kokoro_hf")
        return config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def engine(self) -> str:
        return self._config.engine

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def update_config(self, config: SpeechConfig) -> None:
        """Adopt new speech settings (used by /tts at runtime)."""
        config = self._canonical_config(config)
        engine_changed = config.engine != self._config.engine
        was_disabled = not self._config.enabled and config.enabled
        self._config = config
        if engine_changed or was_disabled:
            self._backend = None
            self._unavailable = False
            self._last_error = None
        log_debug(f"Speech: config updated engine={config.engine} enabled={config.enabled}")

    def switch_engine(self, engine: str) -> bool:
        """Switch between 'kokoro' and 'chatterbox' at runtime."""
        engine = engine.strip().lower()
        if engine == "kokoro":
            engine = "kokoro_hf"
        if engine not in {"kokoro_hf", "kokoro_local", "chatterbox"}:
            return False
        self._config = replace(self._config, engine=engine)
        self._backend = None
        self._unavailable = False
        self._last_error = None
        log_info(f"Speech: engine switched to {engine}")
        return True

    def say(self, text: str) -> None:
        """Speak *text*; failures are logged and never crash the REPL."""
        text = strip_markdown_for_speech(conversationalize_for_speech(text))
        if not self._config.enabled or not text.strip():
            return
        if self._unavailable:
            return
        try:
            if self._backend is None:
                if self._config.engine == "kokoro_hf":
                    self._backend = KokoroHFBackend(self._config)
                elif self._config.engine == "kokoro_local":
                    self._backend = KokoroLocalBackend(self._config)
                else:
                    self._backend = ChatterboxBackend(self._config)
            log_debug(f"Speech: speaking {len(text)} chars via {self._backend.name}")
            self._backend.speak(text)
            self._last_error = None
        except RuntimeError as exc:
            # Missing dependencies, audio devices, and similar configuration
            # problems are retained for /tts status and written to the log.
            self._unavailable = True
            self._last_error = str(exc)
            log_error(f"Speech: {exc}. Run /tts status for details; retry with /tts on.")
        except ImportError as exc:
            self._unavailable = True
            self._last_error = str(exc)
            log_error(
                f"Speech: engine '{self._config.engine}' unavailable ({exc}). "
                "Install its extras or /tts off."
            )
        except Exception as exc:
            self._unavailable = True
            self._last_error = f"{type(exc).__name__}: {exc}"
            log_error(f"Speech: playback failed: {type(exc).__name__}: {exc}")
