"""What this user has installed and enabled: ``<plugins-dir>/state.json``.

The plugins dir is ``<data-dir>/plugins``. The state file is written only by
this module, atomically (temp file + ``os.replace``), and a malformed file is
a boot-stopping :class:`StateFileError` naming the path, the same rule the
feature-flag file follows: a half-readable state file must not quietly read
as "nothing installed" and lose a user's modules.

Enabling is a DECISION THE USER MAKES, and it takes effect at the next
engine start; nothing here loads code. Labs is opt-in per user
(``labs_opt_in``), and a tier-2 module also records that its notice was
acknowledged (PLUG-6522d).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from apps.plugins.regular import open_regular_file

PLUGINS_DIRNAME: str = "plugins"
STATE_FILENAME: str = "state.json"
STATE_SCHEMA: int = 1
SOURCES: tuple[str, ...] = ("store:stable", "store:labs", "sideload")


class StateFileError(ValueError):
    pass


@dataclass(frozen=True)
class InstalledPlugin:
    plugin_id: str
    version: str
    enabled: bool
    source: str
    installed_at: str
    granted_permissions: tuple[str, ...]
    notice_acknowledged: bool = False
    #: sha256 of the exact notice text accepted, so a changed notice in the same
    #: version is never covered by consent given to the old one.
    accepted_notice_sha256: str = ""
    #: Fresh for every install; lets crash recovery tell whether this record landed,
    #: and the store tell a same-version reinstall from the build that is running.
    install_id: str = ""


@dataclass(frozen=True)
class PluginState:
    labs_opt_in: bool = False
    plugins: dict[str, InstalledPlugin] = field(default_factory=dict)
    #: Every store channel whose signed catalog has verified here. Only grows: a
    #: store plugin loads only while each of these still verifies, so deleting a
    #: channel's cache cannot drop the revocations it carried.
    blocklist_channels: frozenset[str] = frozenset()

    def with_plugin(self, plugin: InstalledPlugin) -> PluginState:
        return replace(self, plugins={**self.plugins, plugin.plugin_id: plugin})

    def without(self, plugin_id: str) -> PluginState:
        rest = {k: v for k, v in self.plugins.items() if k != plugin_id}
        return replace(self, plugins=rest)


def plugins_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / PLUGINS_DIRNAME


def _state_path(root: Path) -> Path:
    """The state file, refused when the plugins folder is a link or not a folder.

    Boot stops on it, as on a malformed file: through a linked folder every
    install, state write and catalog refresh would land in the link's target.
    """
    if root.is_symlink():
        raise StateFileError(f"{root} is a symlink; the plugins folder must be a real folder")
    if root.exists() and not root.is_dir():
        raise StateFileError(f"{root} is not a folder; the plugins folder must be a real folder")
    return root / STATE_FILENAME


def _flag(value: Any, key: str, path: Path) -> bool:
    """A JSON boolean, or StateFileError: bool("false") is True, so nothing is coerced."""
    if not isinstance(value, bool):
        raise StateFileError(f"{path}: {key!r} must be true or false, got {value!r}")
    return value


def _plugin(raw: Any, path: Path) -> InstalledPlugin:
    try:
        plugin = InstalledPlugin(
            plugin_id=str(raw["plugin_id"]),
            version=str(raw["version"]),
            enabled=_flag(raw["enabled"], "enabled", path),
            source=str(raw["source"]),
            installed_at=str(raw["installed_at"]),
            granted_permissions=tuple(str(p) for p in raw["granted_permissions"]),
            notice_acknowledged=_flag(raw.get("notice_acknowledged", False), "notice_acknowledged", path),
            accepted_notice_sha256=str(raw.get("accepted_notice_sha256", "")),
            install_id=str(raw.get("install_id", "")),
        )
    except (KeyError, TypeError) as exc:
        raise StateFileError(f"{path}: a plugin row is malformed ({exc})") from exc
    if plugin.source not in SOURCES:
        raise StateFileError(f"{path}: unknown source {plugin.source!r}")
    _check_names(plugin, path)
    return plugin


def _check_names(plugin: InstalledPlugin, path: Path) -> None:
    """The id and version name folders that boot cleanup keeps; a bad one would delete every real version."""
    from apps.plugins.manifest import ManifestError, is_plugin_id, semver_key  # manifest imports this module

    if not is_plugin_id(plugin.plugin_id):
        raise StateFileError(f"{path}: {plugin.plugin_id!r} is not a plugin id")
    try:
        semver_key(plugin.version)
    except ManifestError as exc:
        raise StateFileError(f"{path}: {plugin.plugin_id} has version {plugin.version!r}, which is not semver") from exc


#: Far above any real install record (about 1 KiB per plugin); a larger file is corrupt.
MAX_STATE_BYTES = 4 * 1024 * 1024


def _read_state_text(path: Path) -> str:
    """The state file's text, refused unread past :data:`MAX_STATE_BYTES` (StateFileError)."""
    with open_regular_file(path) as handle:
        data = handle.read(MAX_STATE_BYTES + 1)
    if len(data) > MAX_STATE_BYTES:
        raise StateFileError(f"{path}: plugin state is larger than {MAX_STATE_BYTES} bytes")
    return data.decode("utf-8")


def load_state(root: Path) -> PluginState:
    path = _state_path(root)
    if not path.exists():
        return PluginState()
    try:
        raw = json.loads(_read_state_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateFileError(f"{path}: unreadable plugin state ({exc})") from exc
    if not isinstance(raw, dict) or raw.get("schema") != STATE_SCHEMA:
        raise StateFileError(f"{path}: plugin state schema must be {STATE_SCHEMA}")
    rows = raw.get("plugins", {})
    if not isinstance(rows, dict):
        raise StateFileError(f"{path}: 'plugins' must be an object")
    plugins = {key: _plugin(value, path) for key, value in rows.items()}
    for key, plugin in plugins.items():
        if key != plugin.plugin_id:
            raise StateFileError(f"{path}: row {key!r} names plugin {plugin.plugin_id!r}")
    return PluginState(
        labs_opt_in=_flag(raw.get("labs_opt_in", False), "labs_opt_in", path),
        plugins=plugins,
        blocklist_channels=_channels(raw.get("blocklist_channels", []), path),
    )


_CHANNELS = frozenset(source.removeprefix("store:") for source in SOURCES if source.startswith("store:"))


def _channels(raw: Any, path: Path) -> frozenset[str]:
    if not isinstance(raw, list) or not all(isinstance(c, str) and c in _CHANNELS for c in raw):
        raise StateFileError(f"{path}: 'blocklist_channels' must list store channels, got {raw!r}")
    return frozenset(raw)


#: Serializes every read-modify-write of the state file in this process. The
#: engine runs sync routes on a thread pool, so two clicks can land at once.
STATE_LOCK = threading.RLock()


def write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` in one step through a temp file no other writer shares."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_limit(channels: frozenset[str]) -> int:
    """The largest body a save may write when ``channels`` is the set it records.

    Each store channel the file does not record yet needs one indented JSON string, plus the list's own
    brackets, so that room is held back until every channel is recorded; a fully recorded file takes the
    whole read cap.
    """
    missing = _CHANNELS - channels
    return MAX_STATE_BYTES - (sum(len(channel) + 8 for channel in missing) + 8 if missing else 0)


def save_state(root: Path, state: PluginState) -> None:
    """Write the state, refusing a body past :func:`write_limit` (StateFileError).

    A state file that exists but does not load is refused, never overwritten: the write would erase
    channels a newer writer recorded and hide the damage boot stops on. The size of a record is bounded
    where it is parsed (the manifest's permissions), so the cap is a backstop, not a budget.
    """
    path = _state_path(root)
    root.mkdir(parents=True, exist_ok=True)
    channels = state.blocklist_channels | load_state(root).blocklist_channels  # a save from older state never drops one
    body = {
        "schema": STATE_SCHEMA,
        "labs_opt_in": state.labs_opt_in,
        "blocklist_channels": sorted(channels),
        "plugins": {
            key: {**asdict(p), "granted_permissions": list(p.granted_permissions)}
            for key, p in sorted(state.plugins.items())
        },
    }
    data = (json.dumps(body, indent=2) + "\n").encode("utf-8")
    limit = write_limit(channels)
    if len(data) > limit:  # refused before the write, so the readable file stays as it was
        raise StateFileError(f"{path}: the state would be {len(data)} bytes, past the {limit}-byte cap")
    write_atomic(path, data)
