"""Build the /tracks/index body two ways, in a fresh process, and compare the bytes (LIBM-172).

The NEW way is the route's own ``track_index_body``: plain rows, validated and
serialized once. The OLD way, kept here as the oracle, is the per-row model path it
replaced: a ``TrackOut`` per row, dumped, then validated again as a
``TrackIndexItemOut``. Both read the same library through the same app, in the same
process, one after the other.

The child needs ``MDT_DATA_DIR`` (and ``HOME`` for a fixture's rekordbox share) in
its environment, like the packaged engine:

    python -m tests.webui.track_index_golden_probe <out.json>

It writes ``{"new": {...}, "old": {...}, "first_difference": int | null, "served": {...}}``
where each side carries the byte count, the sha1, the CPU seconds the build took, and
the library row count. Run against a real library copy it is also the handler's CPU
timing: ``new.cpu_s`` against ``old.cpu_s`` on the same rows.

``served`` (issue #5987) names, per row path, the fields that path hands its model
itself rather than leaving them to a model default: ``listing`` is ``GET /tracks``
(``_listing_items``), ``index`` is ``GET /tracks/index`` (``_index_rows``) and
``probe`` is the oracle below. Each path is a list of the distinct field sets its rows
were built with, so a path that builds rows two ways shows two sets. The oracle does
not call ``_listing_row_from_builder``; listing and index do.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from starlette.requests import Request

from apps.webui.server import rb_vendor
from apps.webui.server.app import create_app
from apps.webui.server.backend import StateBackend, TrackFilter
from apps.webui.server.deps import get_library_data_dir
from apps.webui.server.models import TrackIndexItemOut, TrackIndexOut
from apps.webui.server.routes.tracks import (
    INDEX_READ_PAGE,
    _index_rows,
    _listing_items,
    _track_to_out,
    track_index_body,
)
from apps.webui.server.sqlite_backend import SqliteBackend


def old_index_items(request: Request, tracks: list[Any]) -> list[TrackIndexItemOut]:
    """One page of the per-row model path: a ``TrackIndexItemOut`` per row (the oracle).

    Independent of ``_listing_row_from_builder``. Production listing and index share
    that helper; this oracle still builds a ``TrackOut`` and then copies the builder
    row field by field, so a wrong mapping in the helper cannot move both sides of
    the byte comparison.
    """
    rows = rb_vendor.build_track_rows(
        tracks,
        jobs_store=getattr(request.app.state, "jobs_store", None),
        data_dir=get_library_data_dir(request),
        state_db_path=Path(request.app.state.state_db_path),
        with_row_assets=False,
    )
    items: list[TrackIndexItemOut] = []
    for track, row in zip(tracks, rows, strict=True):
        base = _track_to_out(
            track,
            has_rb_mapping=row["has_rb_mapping"],
            lyrics_available=False,
            auto_cues_available=False,
            stems_available=False,
            artwork_available=row["artwork_available"],
        ).model_dump()
        base["play_count"] = int(row.get("play_count") or 0)
        base["bpm"] = row["bpm"]
        base["key"] = row["key"]
        # CMDK-04: the independent oracle includes the index comment field.
        base["comments"] = row.get("comments")
        items.append(
            TrackIndexItemOut(
                **base,
                preview_b64=row["preview_b64"],
                preview_max=row["preview_max"],
                file_availability=row["file_availability"],
                file_exists=row["file_exists"],
                is_remote=bool(row.get("is_remote")),
                is_streaming=row["is_streaming"],
                streaming_provider=row["streaming_provider"],
                has_remote_copy=bool(row["has_remote_copy"]),
                deck_load_refusal=row.get("deck_load_refusal"),
                cloud_transfer=row["cloud_transfer"],
                quality=row["quality"],
                vocals=row["vocals"],
                stems=row["stems"],
                artwork_status=row["artwork_status"],
                energy=row["energy"],
                energy_source=row["energy_source"],
                energy_reason=row["energy_reason"],
                key_status=row["key_status"],
                key_reason=row.get("key_reason"),
                bpm_status=row["bpm_status"],
                bpm_reason=row.get("bpm_reason"),
                bpm_source=row.get("bpm_source"),
                bpm_method=row.get("bpm_method"),
                bpm_confidence=row.get("bpm_confidence"),
                bpm_confidence_error=row.get("bpm_confidence_error"),
                # GRID-OCTAVE-02 and SRCFLAG-01: served on the list rows too.
                bpm_disputed=row.get("bpm_disputed", False),
                bpm_alternatives=row.get("bpm_alternatives", []),
                bpm_pick=row.get("bpm_pick"),
                source_flags=row["source_flags"],
                loudness_status=row["loudness_status"],
                loudness_reason=row.get("loudness_reason"),
                lyrics=row.get("lyrics"),
                grid_quality=row["grid_quality"],
                analysis_hints=row["analysis_hints"],
                is_remix=bool(row.get("is_remix")),
                is_radio_edit=bool(row.get("is_radio_edit")),
                genre=row.get("genre"),
                genre_source=row.get("genre_source"),
                genre_reason=row.get("genre_reason"),
                genre_guess=row.get("genre_guess"),
                edit_id=None,
                in_ms=None,
                out_ms=None,
                fx=None,
                edit_label=None,
                created_by=None,
                is_edit=False,
                valid=True,
                offload=row["offload"],
            )
        )
    return items


def old_index_body(request: Request, backend: StateBackend) -> bytes:
    """The per-row model path the index used before plain rows (the oracle)."""
    revision = backend.library_revision()
    items: list[TrackIndexItemOut] = []
    cursor: str | None = None
    while True:
        page = backend.list_tracks(TrackFilter(cursor=cursor, limit=INDEX_READ_PAGE))
        items.extend(old_index_items(request, page.items))
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return TrackIndexOut.model_validate({"revision": revision, "items": items}).model_dump_json(by_alias=True).encode()


def _distinct(field_sets: list[set[str]]) -> list[list[str]]:
    return [list(fields) for fields in sorted({tuple(sorted(f)) for f in field_sets})]


def served_fields(request: Request, backend: StateBackend) -> dict[str, list[list[str]]]:
    """The fields each of the three row paths supplies, over the first read page."""
    tracks = backend.list_tracks(TrackFilter(limit=INDEX_READ_PAGE)).items
    listing, _hidden = _listing_items(request, tracks, "all", True)
    index, _hidden = _index_rows(request, tracks, True)
    probe = old_index_items(request, tracks)
    return {
        "listing": _distinct([set(item.model_fields_set) for item in listing]),
        "index": _distinct([set(row) for row in index]),
        "probe": _distinct([set(item.model_fields_set) for item in probe]),
    }


def _timed(build: Callable[[Request, StateBackend], bytes], request: Request, backend: StateBackend) -> tuple[bytes, dict[str, Any]]:
    started = time.process_time()
    body = build(request, backend)
    cpu_s = time.process_time() - started
    rows = len(json.loads(body)["items"])
    return body, {"bytes": len(body), "sha1": hashlib.sha1(body, usedforsecurity=False).hexdigest(),
                  "cpu_s": round(cpu_s, 3), "rows": rows}


def _first_difference(a: bytes, b: bytes) -> int | None:
    if a == b:
        return None
    return next((i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)))


def main(out_path: str) -> None:
    state_db = Path(os.environ["MDT_DATA_DIR"]) / "state" / "state.db"
    backend = SqliteBackend(state_db)
    app = create_app(backend=backend, bind_host="127.0.0.1", hostname="test-host",
                     state_db_path=str(state_db), mount_frontend=False)
    with TestClient(app, base_url="http://127.0.0.1"):
        request = Request({"type": "http", "app": app, "headers": [], "method": "GET", "path": "/",
                           "query_string": b""})
        old_index_body(request, backend)  # warm both paths' caches alike before timing
        new_body, new = _timed(track_index_body, request, backend)
        old_body, old = _timed(old_index_body, request, backend)
        served = served_fields(request, backend)
    at = _first_difference(new_body, old_body)
    result: dict[str, Any] = {"new": new, "old": old, "first_difference": at, "served": served}
    if at is not None:
        result["context"] = {"new": new_body[max(0, at - 80):at + 80].decode(errors="replace"),
                             "old": old_body[max(0, at - 80):at + 80].decode(errors="replace")}
    Path(out_path).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
