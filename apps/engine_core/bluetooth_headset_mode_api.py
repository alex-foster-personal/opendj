"""``GET /api/v1/audio-headset-mode`` -- Bluetooth headphones in call mode.

A web page cannot read a device's transport or sample rate without first
opening the microphone, and opening a headset's microphone is exactly what
flips it to call mode. The engine reads CoreAudio's PROPERTIES instead, which
opens no stream and needs no microphone permission.

`supported: false` is NOT "headphones fine" and is never rendered as a pass:
off macOS, or when CoreAudio refuses a read, the route says it could not look.
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable

from fastapi import FastAPI
from pydantic import BaseModel

from apps.shared.bluetooth_headset_mode import (
    AudioDeviceReading,
    HeadsetModeVerdict,
    classify_headset_mode,
)

AUDIO_HEADSET_MODE_PATH: str = "/api/v1/audio-headset-mode"

_K_CF_STRING_ENCODING_UTF8 = 0x08000100
_K_AUDIO_OBJECT_SYSTEM_OBJECT = 1


def _fourcc(code: bytes) -> int:
    return int.from_bytes(code, "big")


_SEL_DEFAULT_OUTPUT = _fourcc(b"dOut")
_SEL_DEFAULT_INPUT = _fourcc(b"dIn ")
_SEL_TRANSPORT = _fourcc(b"tran")
_SEL_NOMINAL_RATE = _fourcc(b"nsrt")
_SEL_NAME = _fourcc(b"lnam")
_SCOPE_GLOBAL = _fourcc(b"glob")

#: `kAudioDeviceTransportType*` codes, named for the wire. Unlisted codes are
#: reported as their four characters so nothing is silently renamed.
_TRANSPORT_NAMES: dict[int, str] = {
    _fourcc(b"blue"): "bluetooth",
    _fourcc(b"blea"): "bluetooth_le",
    _fourcc(b"bltn"): "built_in",
    _fourcc(b"usb "): "usb",
    _fourcc(b"hdmi"): "hdmi",
    _fourcc(b"dprt"): "displayport",
    _fourcc(b"airp"): "airplay",
    _fourcc(b"virt"): "virtual",
    _fourcc(b"grup"): "aggregate",
    _fourcc(b"thun"): "thunderbolt",
}


class _PropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


class AudioDeviceOut(BaseModel):
    device_id: int
    name: str
    transport: str
    nominal_sample_rate_hz: float


class AudioHeadsetModeOut(BaseModel):
    supported: bool
    verdict: str
    output: AudioDeviceOut | None
    input: AudioDeviceOut | None
    error: str | None = None


DeviceReader = Callable[[], tuple[AudioDeviceReading | None, AudioDeviceReading | None]]


class _CoreAudio:
    """Read-only CoreAudio property access; opens no stream."""

    def __init__(self) -> None:
        # Literal paths: the payload build keys its runtime-load allowlist by them (INSTALL-11).
        self._ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        self._cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._get = self._ca.AudioObjectGetPropertyData
        self._get.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_PropertyAddress),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self._get.restype = ctypes.c_int32
        self._cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        self._cf.CFStringGetCString.restype = ctypes.c_bool
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]

    def _read(self, object_id: int, selector: int, out: ctypes._SimpleCData) -> None:
        address = _PropertyAddress(selector, _SCOPE_GLOBAL, 0)
        size = ctypes.c_uint32(ctypes.sizeof(out))
        status = self._get(object_id, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(out))
        if status != 0:
            raise OSError(f"AudioObjectGetPropertyData({selector:#x}) on {object_id} failed: {status}")

    def _name(self, device_id: int) -> str:
        ref = ctypes.c_void_p()
        self._read(device_id, _SEL_NAME, ref)
        if not ref.value:
            return ""
        try:
            buf = ctypes.create_string_buffer(512)
            if not self._cf.CFStringGetCString(ref, buf, len(buf), _K_CF_STRING_ENCODING_UTF8):
                raise OSError(f"device {device_id} name is not representable as UTF-8 in 512 bytes")
            return buf.value.decode("utf-8")
        finally:
            self._cf.CFRelease(ref)

    def default_device(self, selector: int) -> AudioDeviceReading | None:
        device_id = ctypes.c_uint32()
        self._read(_K_AUDIO_OBJECT_SYSTEM_OBJECT, selector, device_id)
        if device_id.value == 0:  # kAudioObjectUnknown: no such default device
            return None
        transport = ctypes.c_uint32()
        self._read(device_id.value, _SEL_TRANSPORT, transport)
        rate = ctypes.c_double()
        self._read(device_id.value, _SEL_NOMINAL_RATE, rate)
        code = transport.value
        return AudioDeviceReading(
            device_id=device_id.value,
            name=self._name(device_id.value),
            transport=_TRANSPORT_NAMES.get(code, code.to_bytes(4, "big").decode("latin-1")),
            nominal_sample_rate_hz=float(rate.value),
        )


def read_default_devices() -> tuple[AudioDeviceReading | None, AudioDeviceReading | None]:
    """The system default (output, input). Raises off macOS or on a refused read."""
    if sys.platform != "darwin":
        raise OSError(f"headset-mode detection reads CoreAudio and needs macOS, not {sys.platform}")
    core = _CoreAudio()
    return core.default_device(_SEL_DEFAULT_OUTPUT), core.default_device(_SEL_DEFAULT_INPUT)


def _device_out(device: AudioDeviceReading | None) -> AudioDeviceOut | None:
    if device is None:
        return None
    return AudioDeviceOut(
        device_id=device.device_id,
        name=device.name,
        transport=device.transport,
        nominal_sample_rate_hz=device.nominal_sample_rate_hz,
    )


def add_audio_headset_mode_route(app: FastAPI, *, read_devices: DeviceReader = read_default_devices) -> None:
    @app.get(
        AUDIO_HEADSET_MODE_PATH,
        response_model=AudioHeadsetModeOut,
        tags=["health"],
        name="audio_headset_mode",
    )
    def audio_headset_mode() -> AudioHeadsetModeOut:
        try:
            output, input_device = read_devices()
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            return AudioHeadsetModeOut(
                supported=False,
                verdict=HeadsetModeVerdict.UNSUPPORTED.value,
                output=None,
                input=None,
                error=str(exc),
            )
        verdict = classify_headset_mode(output, input_device)
        return AudioHeadsetModeOut(
            supported=verdict is not HeadsetModeVerdict.UNSUPPORTED,
            verdict=verdict.value,
            output=_device_out(output),
            input=_device_out(input_device),
            error="no default output device" if output is None else None,
        )


__all__ = [
    "AUDIO_HEADSET_MODE_PATH",
    "AudioDeviceOut",
    "AudioHeadsetModeOut",
    "add_audio_headset_mode_route",
    "read_default_devices",
]
