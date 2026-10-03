"""OneLibrary (``exportLibrary.db``) overlay writer.

CAT-06 Prototype B: produce a valid Rekordbox 7 **One Library** (aka
*Device Library Plus*) SQLCipher file that round-trips through the same
reader without data loss.

This writer does **not** produce a gig stick: it cannot write ``export.pdb``,
``USBANLZ/``, or audio. It overlays OneLibrary only. Gig-stick value
verification after a rekordbox export is
``python -m apps.sync.usb.verify --pioneer-export``.

The database handle is our own (:mod:`apps.sync.usb.pioneer.onelibrary`,
SQLCipher via ``sqlcipher3``). It replaced the ``rbox`` wheel on Thu 1 Oct
2026 because rbox 0.1.6+ is GPL-3.0-only; see
``research/licensing/2026-10-01-rbox-replacement.md``.

We never build a OneLibrary from scratch: users always have a
Rekordbox-produced ``exportLibrary.db`` that we copy as a scaffold and then
mutate. The writer therefore takes a *template* DB as input (typically the
user's last export), overlays playlists + content updates, and writes the
result to the target path.

Public API
----------

:func:`write_onelibrary` -- copy a template OneLibrary, overlay
tracks+playlists, and write to the target path.

:class:`OneLibraryWriteResult` -- summary of the write (tracks written,
playlists written, etc.).

All functions raise :class:`OneLibraryWriteError` on failure.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .onelibrary import BACKEND, SQLCIPHER_AVAILABLE, SQLCIPHER_IMPORT_ERROR, OneLibrary

WRITER_AVAILABLE = SQLCIPHER_AVAILABLE
WRITER_IMPORT_ERROR: str | None = SQLCIPHER_IMPORT_ERROR


__all__ = [
    "BACKEND",
    "WRITER_AVAILABLE",
    "WRITER_IMPORT_ERROR",
    "OneLibraryWriteError",
    "OneLibraryWriteResult",
    "PlaylistSpec",
    "TrackUpdate",
    "write_onelibrary",
]


class OneLibraryWriteError(RuntimeError):
    """Raised when the OneLibrary writer cannot complete a write."""


@dataclasses.dataclass(frozen=True)
class TrackUpdate:
    """Metadata overlay to apply to an existing track in the template DB.

    Only the fields set to a non-``None`` value are written.  ``id`` is
    the primary key of the track in the template DB (i.e. the
    ``content.id`` column).
    """

    id: int
    title: str | None = None
    rating: int | None = None
    bpmx100: int | None = None
    dj_comment: str | None = None
    color_id: int | None = None

    def to_overlay(self) -> dict[str, Any]:
        """Return the fields that should be written to the content row."""
        overlay: dict[str, Any] = {}
        for field in dataclasses.fields(self):
            if field.name == "id":
                continue
            value = getattr(self, field.name)
            if value is not None:
                overlay[field.name] = value
        return overlay


@dataclasses.dataclass(frozen=True)
class PlaylistSpec:
    """A playlist to (re)create in the output DB.

    ``track_ids`` are primary keys of existing content rows in the
    template DB (matching ``TrackUpdate.id`` semantics).
    """

    name: str
    track_ids: Sequence[int]
    parent_id: int | None = None


@dataclasses.dataclass(frozen=True)
class OneLibraryWriteResult:
    """Summary of a :func:`write_onelibrary` call."""

    output_path: Path
    tracks_updated: int
    playlists_written: int
    playlist_ids: tuple[int, ...]
    output_size_bytes: int
    backend: str


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


# Mirrors tests/fixtures/_resolver.py's external-host default and env var
# override rather than importing it: apps/ may not import tests/
# (.importlinter's production-code-never-imports-tests contract is a hard
# zero-tolerance gate, and its one existing exception is scoped to
# differ.py only).
_DEFAULT_EXTERNAL_FIXTURE_HOST = Path("/Volumes/LaCie/music-dj-tools-fixtures")
_TESTS_FIXTURES_DIR = Path(__file__).resolve().parents[4] / "tests" / "fixtures"


def _is_under_fixture_root(path: Path) -> bool:
    """True if ``path`` falls under a canonical fixture root.

    Covers three cases, matching ``fixture_path()``'s three ways to hand
    back a directory (PR #718 review):

    1. The in-repo ``tests/fixtures/`` tree.
    2. The resolved external fixture host (LaCie or ``MUX_FIXTURE_HOST``):
       a fixture resolved through the external host no longer has "tests"
       and "fixtures" path components, so a plain substring check alone
       misses it.
    3. An ad-hoc contributor symlink under ``tests/fixtures/``: its target
       can be any directory, so the guard also treats whatever a
       ``tests/fixtures/*`` symlink resolves to (or any path beneath it)
       as a fixture root. This only reads directory entries and symlink
       targets on disk -- no Python import of ``tests/`` -- so it stays
       inside the .importlinter boundary above.
    """
    if any(part == "fixtures" for part in path.parts) and "tests" in path.parts:
        return True
    external_host = Path(
        os.environ.get("MUX_FIXTURE_HOST", str(_DEFAULT_EXTERNAL_FIXTURE_HOST))
    )
    if path.is_relative_to(external_host.resolve()):
        return True
    if _TESTS_FIXTURES_DIR.is_dir():
        for entry in _TESTS_FIXTURES_DIR.iterdir():
            if not entry.is_symlink():
                continue
            target = entry.resolve()
            if target.is_dir() and (path == target or path.is_relative_to(target)):
                return True
    return False


def write_onelibrary(
    *,
    template_path: str | Path,
    output_path: str | Path,
    track_updates: Iterable[TrackUpdate] | None = None,
    playlists: Iterable[PlaylistSpec] | None = None,
    overwrite: bool = True,
) -> OneLibraryWriteResult:
    """Produce a OneLibrary DB by overlaying updates on a template copy.

    Parameters
    ----------
    template_path:
        Path to an existing Rekordbox-produced ``exportLibrary.db``
        (typically a fixture or the user's last real export).  Must be
        a valid SQLCipher OneLibrary file.
    output_path:
        Destination path (will be overwritten if ``overwrite`` is True).
        The directory is created if missing.
    track_updates:
        Metadata overlays to apply to existing content rows.
    playlists:
        New playlists to create.  They are appended to the existing
        playlist set; sequence numbers are auto-assigned after the
        current max ``seq``.
    overwrite:
        If False and ``output_path`` exists, raise
        :class:`OneLibraryWriteError`.

    Returns
    -------
    :class:`OneLibraryWriteResult`
        Metadata about the write.

    Raises
    ------
    OneLibraryWriteError
        On any of: sqlcipher3 unavailable; template missing; output exists +
        no overwrite; the database rejects an overlay.
    """
    if not WRITER_AVAILABLE:
        raise OneLibraryWriteError(
            f"OneLibrary writer unavailable ({WRITER_IMPORT_ERROR}). "
            "Install the repository dependencies (sqlcipher3 via pyrekordbox)."
        )

    template = Path(template_path).resolve()
    output = Path(output_path).resolve()

    if not template.is_file():
        raise OneLibraryWriteError(f"Template OneLibrary not found: {template}")
    if output.exists() and not overwrite:
        raise OneLibraryWriteError(f"Output path exists and overwrite=False: {output}")

    if _is_under_fixture_root(output):
        raise OneLibraryWriteError(
            f"refusing output_path under a fixture root: {output}"
        )

    # --- Fixture-safety guards (defense-in-depth) --------------------
    # The writer opens read/write: every open may write to the DB and
    # materialize -shm/-wal sidecars next to it. If the
    # caller accidentally points template_path at the committed fixture
    # (or passes the same path as template and output), that will mutate
    # files under tests/fixtures/ on every run. Catch both cases loudly.
    if template == output:
        raise OneLibraryWriteError(
            "template_path must not equal output_path: the writer would open "
            "the template in-place and corrupt it "
            f"(got {template})"
        )
    # A canonical fixture root (in-repo tests/fixtures/ or the resolved
    # external host) is a sentinel: the fixture tree lives there and must
    # never be opened read/write. Callers should copy the fixture into
    # tmp_path and pass THAT path instead.
    if _is_under_fixture_root(template):
        raise OneLibraryWriteError(
            "refusing to operate on fixture path: template_path is "
            f"under a fixture root ({template}). Copy the fixture to a "
            "scratch directory (e.g. tmp_path) first and pass that "
            "copy as template_path."
        )

    _ensure_parent(output)
    # Use shutil.copyfile (not copy2) so we inherit a clean mtime and
    # don't carry UNIX permissions from the fixture.
    shutil.copyfile(template, output)
    # Carry an un-checkpointed -wal along with the main file: copying the
    # main file alone silently drops the pending pages (76 rows on the big
    # fixture; docs/solutions/database-issues/
    # wal-checkpoint-missing-rows-pioneer-usb-writer-20260417.md). The
    # checkpoint after the overlays folds them into the output.
    for suffix in ("-wal", "-shm"):
        side = output.with_name(output.name + suffix)
        if side.exists():
            side.unlink()
        src_side = template.with_name(template.name + suffix)
        if src_side.is_file():
            shutil.copyfile(src_side, side)

    try:
        db = OneLibrary(output)
    except Exception as exc:
        raise OneLibraryWriteError(
            f"Failed to open template copy at {output}: {exc}"
        ) from exc

    tracks_updated = 0
    playlist_ids: list[int] = []

    try:
        # --- Track metadata overlays --------------------------------------
        for update in track_updates or ():
            overlay = update.to_overlay()
            if not overlay:
                continue
            try:
                content = db.get_content_by_id(update.id)
            except Exception as exc:
                raise OneLibraryWriteError(
                    f"Unable to read content id={update.id} from template: {exc}"
                ) from exc
            if content is None:
                raise OneLibraryWriteError(
                    f"Content id={update.id} not present in template DB"
                )
            for key, value in overlay.items():
                content[key] = value
            try:
                db.update_content(content)
            except Exception as exc:
                raise OneLibraryWriteError(
                    f"update_content(id={update.id}) failed: {exc}"
                ) from exc
            tracks_updated += 1

        # --- New playlists -----------------------------------------------
        # Each spec is appended after its siblings, and its tracks are appended
        # in spec order (seq=None asks the handle for max + 1 each time).
        for spec in playlists or ():
            try:
                playlist = db.create_playlist(spec.name, spec.parent_id)
            except Exception as exc:
                raise OneLibraryWriteError(
                    f"create_playlist(name={spec.name!r}) failed: {exc}"
                ) from exc
            playlist_ids.append(int(playlist["id"]))
            for track_id in spec.track_ids:
                try:
                    db.create_playlist_content(int(playlist["id"]), int(track_id))
                except Exception as exc:
                    raise OneLibraryWriteError(
                        f"create_playlist_content(pl={playlist['id']}, "
                        f"content={track_id}) failed: {exc}"
                    ) from exc
        db.checkpoint()
    finally:
        db.close()

    size_bytes = output.stat().st_size
    return OneLibraryWriteResult(
        output_path=output,
        tracks_updated=tracks_updated,
        playlists_written=len(playlist_ids),
        playlist_ids=tuple(playlist_ids),
        output_size_bytes=size_bytes,
        backend=BACKEND,
    )


def read_playlist_roundtrip(
    *,
    onelibrary_path: str | Path,
    playlist_id: int,
) -> Mapping[str, Any]:
    """Read back a playlist + its tracks from a written OneLibrary.

    Used by the Prototype B round-trip tests to independently verify
    what was written.  Returned dict shape::

        {
            "id": int,
            "name": str,
            "seq": int,
            "tracks": [
                {"id": int, "title": str | None, "bpmx100": int | None,
                 "rating": int | None, "path": str | None},
                ...
            ],
        }
    """
    if not WRITER_AVAILABLE:
        raise OneLibraryWriteError(
            f"OneLibrary reader unavailable ({WRITER_IMPORT_ERROR})."
        )
    with OneLibrary(onelibrary_path) as db:
        playlist = db.get_playlist_by_id(int(playlist_id))
        if playlist is None:
            raise OneLibraryWriteError(
                f"playlist id={playlist_id} not found in {onelibrary_path}"
            )
        contents = db.get_playlist_contents(int(playlist_id))
    return {
        "id": int(playlist["id"]),
        "name": str(playlist["name"]),
        "seq": int(playlist["seq"]),
        "tracks": [
            {
                "id": int(c["id"]),
                "title": c.get("title"),
                "bpmx100": c.get("bpmx100"),
                "rating": c.get("rating"),
                "path": c.get("path"),
            }
            for c in contents
        ],
    }


__all__.append("read_playlist_roundtrip")
