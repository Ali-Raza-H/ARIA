from types import ModuleType
from typing import Any

from aria.config import SpeechConfig
from aria.speech import KokoroHFBackend, KokoroLocalBackend, SpeechController


def test_speech_controller_selects_hosted_kokoro_backend() -> None:
    controller = SpeechController(SpeechConfig(enabled=True, engine="kokoro_hf"))

    controller.say("")

    assert controller._backend is None
    controller._backend = KokoroHFBackend(SpeechConfig())
    assert controller._backend.name == "kokoro_hf"


def test_speech_controller_selects_local_kokoro_backend() -> None:
    controller = SpeechController(SpeechConfig(enabled=True, engine="kokoro_local"))

    controller._backend = KokoroLocalBackend(SpeechConfig(engine="kokoro_local"))

    assert controller._backend.name == "kokoro_local"


def test_legacy_kokoro_runtime_switch_uses_hosted_mode() -> None:
    controller = SpeechController(SpeechConfig())

    assert controller.switch_engine("kokoro")
    assert controller._config.engine == "kokoro_hf"


def test_local_kokoro_converts_and_plays_generated_audio(monkeypatch) -> None:
    played: list[tuple[Any, int]] = []

    class FakeTensor:
        size = 3

        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return self

        def squeeze(self):
            return self

    class FakePipeline:
        def __call__(self, text: str, **kwargs: Any):
            assert text == "Hello"
            assert kwargs["voice"] == "af_heart"
            yield "Hello", "phonemes", FakeTensor()

    kokoro_module = ModuleType("kokoro")
    kokoro_module.KPipeline = lambda lang_code: FakePipeline()  # type: ignore[attr-defined]
    sounddevice_module = ModuleType("sounddevice")
    sounddevice_module.query_devices = lambda kind=None: {"name": "test", "max_output_channels": 2}  # type: ignore[attr-defined]
    sounddevice_module.check_output_settings = lambda **kwargs: None  # type: ignore[attr-defined]
    sounddevice_module.play = lambda audio, samplerate, blocking=False: played.append((audio, samplerate))  # type: ignore[attr-defined]
    sounddevice_module.wait = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "kokoro", kokoro_module)
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", sounddevice_module)

    KokoroLocalBackend(
        SpeechConfig(enabled=True, engine="kokoro_local", voice="af_heart")
    ).speak("Hello")

    assert len(played) == 1
    assert played[0][1] == 24_000
