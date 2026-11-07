"""Source flags (SRCFLAG-01..03): tracks whose file may not be the recording it claims.

  GET  /api/v1/source-flags             flagged tracks from the last scan
  GET  /api/v1/source-flags/{id}        one track's flags from the last scan
  POST /api/v1/source-flags/scan        rescan the library and store the result

Flag-only: nothing here refuses, moves or rewrites a file. The listing rows
carry the same flags (``source_flags``), shown in the Err column and on the
cell hover. CLI twin: ``opendj track source-flags``; MCP: ``library_source_flags``.

-Claude
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from apps.adapters.rekordbox import config
from apps.reconcile.http import read_store, scan, write_store
from apps.shared.events import publish

router = APIRouter(prefix="/source-flags", tags=["source-flags"])
MOUNT_UNDER_API_PREFIX = True

CodeFilter = Literal["all", "length_mismatch", "stems_other_recording", "hardlinked_write"]


def _data_dir() -> Path:
    return config.STATE_DB.parent.parent


def _store() -> dict[str, Any]:
    try:
        document = read_store(_data_dir())
    except ValueError as exc:
        raise HTTPException(status_code=500, detail={"code": "SOURCE_FLAGS_STORE_DAMAGED", "message": str(exc)}) from exc
    if document is None:
        raise HTTPException(status_code=404, detail={
            "code": "SOURCE_FLAGS_NOT_SCANNED",
            "message": "no source-flags scan has run; POST /api/v1/source-flags/scan",
        })
    return document


def _counts(flags: dict[str, list[dict[str, str]]]) -> dict[str, int]:
    return dict(Counter(flag["code"] for found in flags.values() for flag in found))


@router.get("")
def list_source_flags(code: CodeFilter = Query("all")) -> dict[str, Any]:
    document = _store()
    flags: dict[str, list[dict[str, str]]] = document["flags"]
    tracks = [
        {"stable_id": sid, "flags": [f for f in found if code == "all" or f["code"] == code]}
        for sid, found in flags.items()
    ]
    tracks = [t for t in tracks if t["flags"]]
    return {"scanned_at": document["scanned_at"], "counts": _counts(flags), "tracks": tracks}


@router.get("/{stable_id}")
def get_source_flags(stable_id: str) -> dict[str, Any]:
    document = _store()
    return {"stable_id": stable_id, "scanned_at": document["scanned_at"], "flags": document["flags"].get(stable_id, [])}


@router.post("/scan")
def scan_source_flags() -> dict[str, Any]:
    found = scan(_data_dir())
    write_store(_data_dir(), found)
    document = _store()
    publish("library.changed", {"kind": "source_flags", "ids": sorted(found)})
    serial = {sid: [asdict(flag) for flag in flags] for sid, flags in found.items()}
    return {"scanned_at": document["scanned_at"], "counts": _counts(serial), "flagged_tracks": len(found)}


__all__ = ["router"]
