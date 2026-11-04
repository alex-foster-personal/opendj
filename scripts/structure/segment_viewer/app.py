"""HTTP surface of the segment viewer. Every view the page draws is a JSON route an agent can read.

Routes (all GET). Library mode, the default view:

- ``/api/config``                            which modes this server has (library, corpus)
- ``/api/library/tracks?q=&limit=``          search the library (artist, title, genre words)
- ``/api/library/bench``                     the bench set, each row with its scores once computed
- ``/api/library/summary``                   per-lane means and pooled label frames over the bench
- ``/api/library/tracks/{id}/segments``      the track document (``library.library_segments``)
- ``/api/library/tracks/{id}/peaks``         3-band peaks, raw uint8, ``columns x 3`` row-major
- ``/api/library/tracks/{id}/audio``         the track's own file, with HTTP Range (local only)

Open test set (Jamendo corpus):

- ``/api/tracks``, ``/api/summary``, ``/api/tracks/{id}/segments|peaks|audio``

Every route also has a CLI twin in ``__main__.py``.

-Claude
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response

from .corpus import Corpus
from .corpus import UnknownTrack as UnknownCorpusTrack
from .library import Library
from .library import UnknownTrack as UnknownLibraryTrack

STATIC = Path(__file__).with_name("static")


def _peaks_response(arr: np.ndarray) -> Response:
    return Response(
        arr.tobytes(order="C"),
        media_type="application/octet-stream",
        headers={"X-Peaks-Shape": f"{arr.shape[0]}x{arr.shape[1]}", "Cache-Control": "max-age=3600"},
    )


def _audio_response(path: Path) -> FileResponse:
    if not path.is_file():
        raise HTTPException(404, "audio file missing")
    return FileResponse(path, headers={"Cache-Control": "max-age=3600"})


def _mount_library(app: FastAPI, lib: Library) -> None:
    def _known(stable_id: str) -> None:
        try:
            lib.track(stable_id)
        except UnknownLibraryTrack:
            raise HTTPException(404, f"unknown library track {stable_id}") from None

    @app.get("/api/library/tracks")
    def search(q: str = Query(""), limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        return {"tracks": lib.search(q, limit)}

    @app.get("/api/library/bench")
    def bench() -> dict[str, Any]:
        return {"tracks": lib.bench()}

    @app.get("/api/library/summary")
    def summary() -> dict[str, Any]:
        return lib.summary()

    @app.get("/api/library/tracks/{stable_id}/segments")
    def segments(stable_id: str) -> dict[str, Any]:
        _known(stable_id)
        try:
            return lib.segments(stable_id)
        except FileNotFoundError as exc:
            raise HTTPException(409, f"cannot analyze {stable_id}: {exc}") from None

    @app.get("/api/library/tracks/{stable_id}/peaks")
    def peaks(stable_id: str) -> Response:
        _known(stable_id)
        lib.segments(stable_id)  # decodes under the track's lock on first use
        return _peaks_response(lib.peaks(stable_id))

    @app.get("/api/library/tracks/{stable_id}/audio")
    def audio(stable_id: str) -> FileResponse:
        _known(stable_id)
        try:
            return _audio_response(lib.audio_path(stable_id))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None


def _mount_corpus(app: FastAPI, corpus: Corpus) -> None:
    def _known(stable_id: str) -> None:
        try:
            corpus.track(stable_id)
        except UnknownCorpusTrack:
            raise HTTPException(404, f"unknown track {stable_id}") from None

    @app.get("/api/tracks")
    def tracks() -> dict[str, Any]:
        return {"tracks": corpus.listing()}

    @app.get("/api/summary")
    def summary() -> dict[str, Any]:
        return corpus.summary()

    @app.get("/api/tracks/{stable_id}/segments")
    def segments(stable_id: str) -> dict[str, Any]:
        _known(stable_id)
        return corpus.segments(stable_id)

    @app.get("/api/tracks/{stable_id}/peaks")
    def peaks(stable_id: str) -> Response:
        _known(stable_id)
        corpus.segments(stable_id)  # decodes the peaks under the track's lock on first use
        return _peaks_response(corpus.peaks(stable_id))

    @app.get("/api/tracks/{stable_id}/audio")
    def audio(stable_id: str) -> FileResponse:
        _known(stable_id)
        return _audio_response(corpus.audio_path(stable_id))


def create_app(corpus: Corpus | None, library: Library | None = None) -> FastAPI:
    app = FastAPI(title="segment viewer")

    @app.get("/")
    def page() -> FileResponse:
        return FileResponse(STATIC / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {
            "library": library is not None,
            "corpus": corpus is not None,
            "corpus_tracks": len(corpus.tracks) if corpus else 0,
        }

    if library is not None:
        _mount_library(app, library)
    if corpus is not None:
        _mount_corpus(app, corpus)
    return app


__all__ = ["create_app"]
