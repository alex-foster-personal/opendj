"""Wake-word detection.

Primary backend: openWakeWord (Apache-2.0). Opt-in backend: Picovoice
Porcupine when ``WAKE_BACKEND=porcupine`` is set (requires
``$PICOVOICE_ACCESS_KEY`` via doppler).

The real backends are imported lazily so this module loads on machines
where the wheels are not installed. Unit tests use the ``StubBackend``
below.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

DEFAULT_WAKE_THRESHOLD: float = 0.5
DEFAULT_WAKE_PHRASE: str = "hey booth"


@runtime_checkable
class WakeWord(Protocol):
    """Protocol every wake-word backend implements.

    A backend eats 16 kHz mono int16 PCM frames (typically 30 ms =
    480 samples = 960 bytes) and returns a confidence in ``[0.0, 1.0]``
    for the current frame. The caller compares against a threshold.
    """

    threshold: float
    frame_samples: int

    def detect(self, frame: bytes) -> float:  # pragma: no cover - protocol
        ...

    def reset(self) -> None:  # pragma: no cover - protocol
        ...


@dataclass
class StubBackend:
    """Deterministic stub used by tests and as a pre-install placeholder.

    Always returns ``confidence`` for the first frame and 0 thereafter
    unless ``always`` is True.
    """

    confidence: float = 0.0
    always: bool = False
    threshold: float = DEFAULT_WAKE_THRESHOLD
    _fired: bool = False
    frame_samples: int = 480

    def detect(self, _frame: bytes) -> float:
        if self.always:
            return self.confidence
        if self._fired:
            return 0.0
        self._fired = True
        return self.confidence

    def reset(self) -> None:
        self._fired = False


class OpenWakeWordBackend:
    """openWakeWord wrapper.

    Imported lazily so the module loads without the wheel. The model is
    bundled at ``apps/voice/models/hey_dj.onnx`` when available; until we
    ship that model we allow ``VOICE_WAKE_MODEL_PATH`` to override.
    """

    frame_samples: int = 1_280

    def __init__(
        self,
        model_path: str | None = None,
        threshold: float = DEFAULT_WAKE_THRESHOLD,
    ) -> None:
        try:
            import openwakeword  # type: ignore[import-not-found]
            from openwakeword.model import Model  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - env-specific
            raise RuntimeError(
                "openwakeword not importable; `pip install openwakeword` "
                "or switch WAKE_BACKEND=stub"
            ) from exc

        self._openwakeword = openwakeword
        resolved = model_path or os.environ.get("VOICE_WAKE_MODEL_PATH")
        # Importing openwakeword is not the same as being able to load a
        # model: its default tflite runtime ships no wheel for every host
        # (macOS arm64 among them) and it raises a bare ValueError there.
        # Translate every construction failure into the same RuntimeError
        # the import guard above raises, so callers have ONE unusable-backend
        # contract to handle instead of the upstream exception zoo.
        try:
            if resolved:
                self._model = Model(wakeword_models=[resolved])
            else:
                # Use whatever prebuilt models ship with openwakeword. This is
                # the fallback for pre-install smoke; production flips
                # VOICE_WAKE_MODEL_PATH to our trained "hey dj" onnx.
                self._model = Model()
        except Exception as exc:  # pragma: no cover - env-specific
            raise RuntimeError(
                f"openwakeword model failed to load ({exc}); install its "
                "runtime, point VOICE_WAKE_MODEL_PATH at an onnx model, or "
                "switch WAKE_BACKEND=stub"
            ) from exc
        self.threshold = threshold

    def detect(self, frame: bytes) -> float:  # pragma: no cover - needs wheel
        # Convert int16 bytes to numpy int16 array; openwakeword expects a
        # 1D np.int16 array. We defer numpy import for the same reason.
        import numpy as np  # type: ignore[import-not-found]

        arr = np.frombuffer(frame, dtype=np.int16)
        preds = self._model.predict(arr)
        return float(max(preds.values())) if preds else 0.0

    def reset(self) -> None:  # pragma: no cover - needs wheel
        try:
            self._model.reset()
        except AttributeError:
            pass


class PorcupineBackend:
    """Porcupine wrapper (opt-in via WAKE_BACKEND=porcupine).

    Requires ``PICOVOICE_ACCESS_KEY`` from doppler and a custom keyword
    file. Raises a clear message if either is missing.
    """

    frame_samples: int = 512

    def __init__(
        self,
        keyword_path: str | None = None,
        access_key: str | None = None,
        threshold: float = DEFAULT_WAKE_THRESHOLD,
    ) -> None:
        key = access_key or os.environ.get("PICOVOICE_ACCESS_KEY")
        if not key:
            raise RuntimeError(
                "PICOVOICE_ACCESS_KEY not set; configure via "
                "`doppler secrets set PICOVOICE_ACCESS_KEY` or use "
                "WAKE_BACKEND=openwakeword (the default)"
            )
        try:
            import pvporcupine  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - env-specific
            raise RuntimeError(
                "pvporcupine not importable; `pip install pvporcupine`"
            ) from exc

        kp = keyword_path or os.environ.get("VOICE_WAKE_KEYWORD_PATH")
        if kp:
            self._porcupine = pvporcupine.create(access_key=key, keyword_paths=[kp])
        else:
            # Porcupine ships a few prebuilt keywords; "computer" is a
            # decent fallback for smoke-testing.
            self._porcupine = pvporcupine.create(access_key=key, keywords=["computer"])
        self.frame_samples = int(self._porcupine.frame_length)
        self.threshold = threshold

    def detect(self, frame: bytes) -> float:  # pragma: no cover - needs wheel
        import numpy as np  # type: ignore[import-not-found]

        arr = np.frombuffer(frame, dtype=np.int16)
        idx = self._porcupine.process(arr)
        return 1.0 if idx >= 0 else 0.0

    def reset(self) -> None:  # pragma: no cover - needs wheel
        pass


def make_backend(env: dict[str, str] | None = None) -> WakeWord:
    """Factory -- picks backend per ``$WAKE_BACKEND`` (default openwakeword).

    ``WAKE_BACKEND=stub`` returns a deterministic ``StubBackend`` suitable
    for tests / CI with no audio wheels installed.
    """
    env = env if env is not None else dict(os.environ)
    backend = (env.get("WAKE_BACKEND") or "openwakeword").lower()
    try:
        threshold = float(env.get("VOICE_WAKE_SENSITIVITY", DEFAULT_WAKE_THRESHOLD))
    except ValueError:
        threshold = DEFAULT_WAKE_THRESHOLD

    if backend == "stub":
        return StubBackend(confidence=1.0, always=False, threshold=threshold)
    if backend == "porcupine":
        return PorcupineBackend(
            keyword_path=env.get("VOICE_WAKE_KEYWORD_PATH"),
            access_key=env.get("PICOVOICE_ACCESS_KEY"),
            threshold=threshold,
        )
    if backend == "openwakeword":
        return OpenWakeWordBackend(
            model_path=env.get("VOICE_WAKE_MODEL_PATH"),
            threshold=threshold,
        )
    raise ValueError(
        f"Unknown WAKE_BACKEND={backend!r}; expected one of openwakeword/porcupine/stub"
    )


def triggered(backend: WakeWord, frames: list[bytes]) -> bool:
    """Run ``backend.detect`` over a stream; return True on first hit."""
    for frame in frames:
        if backend.detect(frame) >= backend.threshold:
            return True
    return False
