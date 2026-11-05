"""Proxy macOS shell output-health probes to the desktop shell loopback server."""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from .utc_timestamps import utc_now_iso_ms

log = logging.getLogger(__name__)

SHELL_JSON_NAME = ".engine.shell.json"
SHELL_TIMEOUT_S = 3.0
# AUDIO-DEVICE-05: blueutil disconnect, reconnect and the CoreAudio reappear
# wait take up to about 30 s on the shell side.
RECONNECT_TIMEOUT_S = 50.0
SHELL_HEALTH_TIMEOUT_REASON = "shell_health_timeout"


class ShellHealthTimeout(RuntimeError):
    """The shell's health server accepted nothing or answered nothing within
    ``SHELL_TIMEOUT_S``. Not a verdict: the probe never ran to completion, so
    the route answers 503 with ``reason: shell_health_timeout``, never a 500
    and never a guessed device state (silver, Tue 6 Oct 2026, HAL overload)."""


def _unknown(reason: str) -> dict[str, Any]:
    return {
        "device_delivering": None,
        "verdict": "unknown",
        "reason": reason,
        "default_device_name": None,
        "default_device_uid": None,
        "io_cycles_advanced": None,
        "hal_overload_recent": None,
        "probe_available": False,
        "checked_at": utc_now_iso_ms(),
    }


@dataclass(frozen=True)
class ShellOutputHealthClient:
    """Read shell health port from ``.engine.shell.json`` and call loopback HTTP."""

    data_dir: Path

    def shell_json_path(self) -> Path:
        return self.data_dir / SHELL_JSON_NAME

    def read_health_port(self) -> int | None:
        path = self.shell_json_path()
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            log.warning("shell json unreadable at %s: %s", path, err)
            return None
        port = payload.get("health_port")
        if not isinstance(port, int) or port <= 0:
            return None
        return port

    def _request(self, method: str, path: str, timeout_s: float = SHELL_TIMEOUT_S) -> dict[str, Any]:
        if sys.platform != "darwin":
            return _unknown("installed macOS shell required for OS output probe")
        port = self.read_health_port()
        if port is None:
            return _unknown("desktop shell health port unavailable (.engine.shell.json missing)")
        url = f"http://127.0.0.1:{port}{path}"
        request = Request(url, method=method, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=timeout_s) as response:
                body = response.read().decode("utf-8")
        except TimeoutError as err:
            raise ShellHealthTimeout(f"{method} {path} timed out after {timeout_s}s: {err}") from err
        except URLError as err:
            if isinstance(err.reason, TimeoutError):
                raise ShellHealthTimeout(
                    f"{method} {path} timed out after {timeout_s}s: {err.reason}"
                ) from err
            return _unknown(f"shell output-health request failed: {err}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as err:
            return _unknown(f"shell output-health returned invalid JSON: {err}")
        if not isinstance(payload, dict):
            return _unknown("shell output-health returned a non-object JSON payload")
        return payload

    def get_output_health(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/audio/output-health")

    def post_switch_output(self) -> dict[str, Any]:
        if sys.platform != "darwin":
            return {
                "cycled": False,
                "error": "installed macOS shell required for output device cycling",
            }
        port = self.read_health_port()
        if port is None:
            return {
                "cycled": False,
                "error": "desktop shell health port unavailable (.engine.shell.json missing)",
            }
        return self._request("POST", "/api/v1/audio/switch-output")

    def post_reconnect_speaker(self) -> dict[str, Any]:
        """AUDIO-DEVICE-05: blueutil reconnect of the main Bluetooth output."""
        if sys.platform != "darwin":
            return {"reconnected": False, "error": "installed macOS shell required to reconnect a Bluetooth speaker"}
        if self.read_health_port() is None:
            return {"reconnected": False, "error": "desktop shell health port unavailable (.engine.shell.json missing)"}
        payload = self._request("POST", "/api/v1/audio/reconnect-speaker", RECONNECT_TIMEOUT_S)
        if "reconnected" not in payload:
            return {"reconnected": False, "error": str(payload.get("reason") or "shell reconnect returned no result")}
        return payload

    def get_input_devices(self) -> dict[str, Any]:
        """AUDIO-DEVICE-04: CoreAudio inputs with transport, for the mic guard."""
        if sys.platform != "darwin":
            return {"inputs": [], "error": "installed macOS shell required to list CoreAudio inputs"}
        if self.read_health_port() is None:
            return {"inputs": [], "error": "desktop shell health port unavailable (.engine.shell.json missing)"}
        payload = self._request("GET", "/api/v1/audio/input-devices")
        if "inputs" not in payload:
            return {"inputs": [], "error": str(payload.get("reason") or "shell input listing returned no result")}
        return payload


def data_dir_from_state_db(state_db_path: Path) -> Path:
    if state_db_path.parent.name == "state":
        return state_db_path.parent.parent
    return state_db_path.parent


def lyric_index_data_dir_from_state_db(state_db_path: Path) -> Path:
    """Root passed to ``apps.lyrics`` cache/index helpers for this app binding."""
    return state_db_path.parent.parent


__all__ = [
    "RECONNECT_TIMEOUT_S",
    "SHELL_HEALTH_TIMEOUT_REASON",
    "ShellHealthTimeout",
    "ShellOutputHealthClient",
    "data_dir_from_state_db",
    "lyric_index_data_dir_from_state_db",
]
