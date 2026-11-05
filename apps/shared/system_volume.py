"""Platform seam: read and set the operating system's master output volume.

One arm per platform, each launching its program by resolved path:

- macOS: ``/usr/bin/osascript`` (pinned; never a PATH lookup).
- Linux: ``wpctl`` (PipeWire), else ``pactl`` (PulseAudio), resolved on PATH
  because distributions install them in different places.
- Windows: the default render endpoint's ``IAudioEndpointVolume`` through
  Core Audio, driven by ``powershell.exe`` under ``%SystemRoot%``.

Volume is an integer percent, 0..100. A host without a working arm raises
:class:`SystemVolumeUnavailable`; nothing here guesses a value. That covers a
Linux sink amplified above 100: the mixer allows it, the API cannot state it,
and clamping would report a level the room is not hearing.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from apps.shared.run_checked import run_checked

OSASCRIPT_PATH: Path = Path("/usr/bin/osascript")
COMMAND_TIMEOUT_S: float = 30.0


class SystemVolumeUnavailable(RuntimeError):
    """The host cannot read or set its output volume; ``reason`` says why."""

    code = "system_volume_unavailable"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"system volume unavailable: {reason}")


@dataclass(frozen=True)
class SystemVolume:
    volume: int
    muted: bool
    backend: str


# ----- shared ---------------------------------------------------------------


def _run(argv: list[str]) -> str:
    return run_checked(argv, label=Path(argv[0]).stem, timeout_s=COMMAND_TIMEOUT_S, error=SystemVolumeUnavailable)


def check_percent(volume: object) -> int:
    if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= 100:
        raise ValueError(f"volume must be an integer percent 0..100, got {volume!r}")
    return volume


# ----- macOS ----------------------------------------------------------------


def _mac_osascript() -> str:
    if not OSASCRIPT_PATH.is_file():
        raise SystemVolumeUnavailable("osascript_missing")
    return str(OSASCRIPT_PATH)


def _mac_get() -> SystemVolume:
    out = _run([_mac_osascript(), "-e", "get volume settings"])
    vol = re.search(r"output volume:(\d+)", out)
    muted = re.search(r"output muted:(true|false)", out)
    if vol is None or muted is None:
        raise SystemVolumeUnavailable("osascript_unparseable")
    return SystemVolume(int(vol.group(1)), muted.group(1) == "true", "osascript")


def _mac_set(volume: int) -> SystemVolume:
    _run([_mac_osascript(), "-e", f"set volume output volume {volume}"])
    return _mac_get()


# ----- Linux ----------------------------------------------------------------


def _linux_tool() -> tuple[str, str]:
    for name in ("wpctl", "pactl"):
        path = shutil.which(name)
        if path is not None:
            return name, path
    raise SystemVolumeUnavailable("neither_wpctl_nor_pactl_on_path")


def _linux_volume(percent: int, muted: bool, backend: str) -> SystemVolume:
    if percent > 100:
        raise SystemVolumeUnavailable(f"sink_above_100_percent:{percent}")
    return SystemVolume(percent, muted, backend)


def _linux_get() -> SystemVolume:
    name, path = _linux_tool()
    if name == "wpctl":
        out = _run([path, "get-volume", "@DEFAULT_AUDIO_SINK@"])
        match = re.search(r"Volume:\s*([\d.]+)", out)
        if match is None:
            raise SystemVolumeUnavailable("wpctl_unparseable")
        return _linux_volume(round(float(match.group(1)) * 100), "[MUTED]" in out, "wpctl")
    vol = re.search(r"(\d+)%", _run([path, "get-sink-volume", "@DEFAULT_SINK@"]))
    if vol is None:
        raise SystemVolumeUnavailable("pactl_unparseable")
    mute = re.fullmatch(r"\s*Mute:\s*(yes|no)\s*", _run([path, "get-sink-mute", "@DEFAULT_SINK@"]))
    if mute is None:
        raise SystemVolumeUnavailable("pactl_mute_unparseable")
    return _linux_volume(int(vol.group(1)), mute.group(1) == "yes", "pactl")


def _linux_set(volume: int) -> SystemVolume:
    name, path = _linux_tool()
    if name == "wpctl":
        _run([path, "set-volume", "@DEFAULT_AUDIO_SINK@", f"{volume / 100:.2f}"])
    else:
        _run([path, "set-sink-volume", "@DEFAULT_SINK@", f"{volume}%"])
    return _linux_get()


# ----- Windows --------------------------------------------------------------

# Core Audio COM interop. Vtable slots before the ones used are placeholders
# and must stay in order: COM dispatches by slot, not by name.
_WIN_CORE_AUDIO = r"""
using System;
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
  [PreserveSig] int f(); [PreserveSig] int g(); [PreserveSig] int h(); [PreserveSig] int i();
  [PreserveSig] int SetMasterVolumeLevelScalar(float level, Guid ctx);
  [PreserveSig] int j();
  [PreserveSig] int GetMasterVolumeLevelScalar(out float level);
  [PreserveSig] int k(); [PreserveSig] int l(); [PreserveSig] int m(); [PreserveSig] int n();
  [PreserveSig] int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, Guid ctx);
  [PreserveSig] int GetMute(out bool mute);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice { [PreserveSig] int Activate(ref Guid id, int ctx, IntPtr p, out IAudioEndpointVolume v); }
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator { [PreserveSig] int f(); [PreserveSig] int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice d); }
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumerator { }
public static class OdjVolume {
  static IAudioEndpointVolume Endpoint() {
    var e = (IMMDeviceEnumerator)(new MMDeviceEnumerator());
    IMMDevice d; Marshal.ThrowExceptionForHR(e.GetDefaultAudioEndpoint(0, 1, out d));
    var iid = typeof(IAudioEndpointVolume).GUID; IAudioEndpointVolume v;
    Marshal.ThrowExceptionForHR(d.Activate(ref iid, 23, IntPtr.Zero, out v));
    return v;
  }
  public static string Get() {
    var v = Endpoint(); float level; bool mute;
    Marshal.ThrowExceptionForHR(v.GetMasterVolumeLevelScalar(out level));
    Marshal.ThrowExceptionForHR(v.GetMute(out mute));
    return ((int)Math.Round(level * 100)).ToString() + " " + (mute ? "1" : "0");
  }
  public static void Set(int percent) {
    Marshal.ThrowExceptionForHR(Endpoint().SetMasterVolumeLevelScalar(percent / 100f, Guid.Empty));
  }
}
"""


def _win_powershell() -> str:
    root = os.environ.get("SYSTEMROOT")
    if not root:
        raise SystemVolumeUnavailable("systemroot_unset")
    path = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not path.is_file():
        raise SystemVolumeUnavailable("powershell_missing")
    return str(path)


def _win_run(statement: str) -> str:
    script = f"$ErrorActionPreference='Stop'; Add-Type -TypeDefinition @'\n{_WIN_CORE_AUDIO}\n'@\n{statement}"
    return _run([_win_powershell(), "-NoProfile", "-NonInteractive", "-Command", script])


def _win_state(statement: str) -> SystemVolume:
    match = re.fullmatch(r"(\d+) ([01])", _win_run(f"{statement}[OdjVolume]::Get()"))
    if match is None:
        raise SystemVolumeUnavailable("core_audio_unparseable")
    return SystemVolume(int(match.group(1)), match.group(2) == "1", "core_audio")


def _win_get() -> SystemVolume:
    return _win_state("")


def _win_set(volume: int) -> SystemVolume:
    return _win_state(f"[OdjVolume]::Set({volume}); ")


# ----- public ---------------------------------------------------------------


def _arm() -> tuple[Callable[[], SystemVolume], Callable[[int], SystemVolume]]:
    if sys.platform == "darwin":
        return _mac_get, _mac_set
    if sys.platform == "win32":
        return _win_get, _win_set
    if sys.platform.startswith("linux"):
        return _linux_get, _linux_set
    raise SystemVolumeUnavailable(f"unsupported_platform:{sys.platform}")


def get_volume() -> SystemVolume:
    return _arm()[0]()


def set_volume(volume: int) -> SystemVolume:
    """Set the master output volume, then return the state read back."""
    return _arm()[1](check_percent(volume))


__all__ = ["SystemVolume", "SystemVolumeUnavailable", "check_percent", "get_volume", "set_volume"]
