"""Loudness sidecar probes for rekordbox USB OneLibrary copies."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from apps.sync.analysis_writeback import _SCALAR_TABLE


def probe_odj_analysis_scalar(db_path: Path) -> dict[int, dict[str, str]]:
    """Return ``{ContentID: {field: value}}`` from an unencrypted sqlite file."""
    if not db_path.is_file():
        return {}
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (_SCALAR_TABLE,),
        ).fetchone()
        if row is None:
            return {}
        out: dict[int, dict[str, str]] = {}
        for content_id, field_name, value in conn.execute(
            f"SELECT ContentID, field, value FROM {_SCALAR_TABLE}"
        ):
            cid = int(content_id)
            out.setdefault(cid, {})[str(field_name)] = str(value)
        return out
    finally:
        conn.close()


def read_scalar_sidecar_from_onelibrary(
    one_lib_path: Path,
) -> tuple[dict[int, dict[str, str]], dict[str, dict[str, Any]], list[str]]:
    """Probe a tempfile copy of ``exportLibrary.db`` for ``odjAnalysisScalar``."""
    unread: list[str] = []
    sidecar: dict[int, dict[str, str]] = {}
    contents_by_filename: dict[str, dict[str, Any]] = {}

    with tempfile.TemporaryDirectory() as tmp:
        copy_path = Path(tmp) / "exportLibrary.db"
        shutil.copy2(one_lib_path, copy_path)

        try:
            from .onelibrary import OneLibrary
        except ImportError as exc:
            unread.append(f"OneLibrary reader unavailable ({exc}); OneLibrary loudness unread")
            return sidecar, contents_by_filename, unread

        try:
            db = OneLibrary(copy_path)
        except Exception as exc:
            unread.append(f"OneLibrary open failed: {exc}")
            return sidecar, contents_by_filename, unread

        try:
            for content in db.get_contents():
                fname = content.get("file_name") or content.get("path")
                if fname:
                    contents_by_filename[str(fname)] = content

            if not db.has_table(_SCALAR_TABLE):
                return sidecar, contents_by_filename, unread
            for content_id, field_name, value in db.conn.execute(
                f"SELECT ContentID, field, value FROM {_SCALAR_TABLE}"
            ):
                cid = int(content_id)
                sidecar.setdefault(cid, {})[str(field_name)] = str(value)
        except Exception as exc:
            unread.append(f"OneLibrary scalar probe failed: {exc}")
        finally:
            db.close()

    return sidecar, contents_by_filename, unread
