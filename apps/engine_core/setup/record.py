"""The durable half of setup state: ``<data_dir>/setup.json``.

Two facts live here and nowhere else: whether the operator dismissed the
first-run wizard, and what the last import actually did. Both have to
survive a restart, and both have to be readable by an agent over HTTP
rather than out of a browser's localStorage -- that is the whole point of
putting them on the engine side.

Malformed JSON RAISES. A setup file we cannot parse is a file someone or
something wrote wrongly; defaulting it away would silently re-show a wizard
the operator already dismissed, or hide an import that already ran.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from pathlib import Path
from typing import Any

RECORD_NAME: str = "setup.json"
RECORD_VERSION: int = 1


class SetupRecordError(ValueError):
    """The on-disk setup record is unreadable. Never silently replaced."""


@dataclasses.dataclass(frozen=True)
class SetupRecord:
    """What setup remembers between engine boots."""

    dismissed: bool = False
    last_import: dict[str, Any] | None = None
    folder_watch_roots: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": RECORD_VERSION,
            "dismissed": self.dismissed,
            "last_import": self.last_import,
            "folder_watch_roots": self.folder_watch_roots,
        }


def record_path(data_dir: Path) -> Path:
    return data_dir / RECORD_NAME


def read(data_dir: Path) -> SetupRecord:
    """Load the record. An ABSENT file is the real first-run state."""
    path = record_path(data_dir)
    if not path.is_file():
        return SetupRecord()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SetupRecordError(f"cannot read setup record {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SetupRecordError(
            f"setup record {path} must be a JSON object, got {type(raw).__name__}"
        )
    version = raw.get("version")
    if version != RECORD_VERSION:
        raise SetupRecordError(
            f"setup record {path} has version {version!r}; this engine writes "
            f"version {RECORD_VERSION}"
        )
    dismissed = raw.get("dismissed", False)
    if not isinstance(dismissed, bool):
        raise SetupRecordError(
            f"setup record {path} field 'dismissed' must be a bool, got "
            f"{dismissed!r}"
        )
    last_import = raw.get("last_import")
    if last_import is not None and not isinstance(last_import, dict):
        raise SetupRecordError(
            f"setup record {path} field 'last_import' must be an object or "
            f"null, got {type(last_import).__name__}"
        )
    folder_watch_roots = raw.get("folder_watch_roots", [])
    if not isinstance(folder_watch_roots, list) or not all(
        isinstance(root, str) for root in folder_watch_roots
    ):
        raise SetupRecordError(
            f"setup record {path} field 'folder_watch_roots' must be a list of strings"
        )
    return SetupRecord(
        dismissed=dismissed,
        last_import=last_import,
        folder_watch_roots=folder_watch_roots,
    )


def write(data_dir: Path, record: SetupRecord) -> SetupRecord:
    """Persist the record atomically, so a crash cannot leave half a file."""
    path = record_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    try:
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return record


def set_dismissed(data_dir: Path, dismissed: bool) -> SetupRecord:
    """Flip the dismissal flag, keeping whatever import history exists."""
    current = read(data_dir)
    return write(
        data_dir,
        SetupRecord(
            dismissed=dismissed,
            last_import=current.last_import,
            folder_watch_roots=current.folder_watch_roots,
        ),
    )


def set_last_import(data_dir: Path, outcome: dict[str, Any]) -> SetupRecord:
    """Record what the last import did, keeping the dismissal flag."""
    current = read(data_dir)
    roots = current.folder_watch_roots
    if outcome.get("kind") == "folder":
        roots = list(dict.fromkeys([*roots, *outcome.get("roots", [])]))
    return write(
        data_dir,
        SetupRecord(
            dismissed=current.dismissed,
            last_import=outcome,
            folder_watch_roots=roots,
        ),
    )


__all__ = [
    "RECORD_NAME",
    "RECORD_VERSION",
    "SetupRecord",
    "SetupRecordError",
    "read",
    "record_path",
    "set_dismissed",
    "set_last_import",
    "write",
]
