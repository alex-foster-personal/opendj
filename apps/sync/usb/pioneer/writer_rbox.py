"""OneLibrary (``exportLibrary.db``) writer via the ``rbox`` Rust crate.

This module is CAT-06 Prototype B: prove that we can produce a valid
Rekordbox 7 **One Library** (aka *Device Library Plus*) SQLCipher file
that round-trips through the same reader without data loss.

This writer does **not** produce a gig stick: it cannot write ``export.pdb``,
``USBANLZ/``, or audio. It overlays OneLibrary only. Gig-stick value
verification after a rekordbox export is
``python -m apps.sync.usb.verify --pioneer-export``.

Background
----------

The OneLibrary ``exportLibrary.db`` is a SQLCipher-encrypted SQLite
database shipped on Pioneer USB/SD exports for CDJ-3000X, OPUS-QUAD,
OMNIS-DUO, and XDJ-AZ players (see Rekordbox 6.8.1 *Device Library Plus
User's Guide*).  The encryption key is a fixed per-feature constant that
is only known to reimplementations of the format, notably Dylan Jones'
`rbox` Rust crate (PyPI ``rbox`` / ``crates.io``) which unlocks the DB
transparently.

rbox-0.1.7 capability matrix (discovered empirically, see tests)
----------------------------------------------------------------

What works (✅)
~~~~~~~~~~~~~~

* Opening an existing ``exportLibrary.db`` via
  :class:`rbox.OneLibrary(path)` — SQLCipher key is resolved internally.
* Reading every top-level table via ``get_contents()``,
  ``get_playlists()``, ``get_artists()`` etc.
* Creating playlists: ``create_playlist(name, parent_id, seq)``.
* Adding existing contents to a playlist:
  ``create_playlist_content(playlist_id, content_id, seq)``.
* Creating artists: ``create_artist(name)``.
* Updating existing content metadata (title, rating, bpmx100, …) via
  ``update_content(content)``. UTF-8 (日本語 / é / emoji 🎧) round-trips
  cleanly.

What does NOT work (❌) in rbox-0.1.7
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* ``OneLibrary.create(path, my_tag_master_dbid)`` — builds a fresh DB
  whose schema uses camelCase FK column names
  (``artist_id_originalArtist``) that do not match the crate's own
  Diesel models (``original_artist_id``). Any subsequent write or a
  plain ``get_contents()`` raises:

        OneLibraryError: Diesel error: no such column:
        content.artist_id_originalArtist

* ``create_content(path)`` — inserts a row that leaves non-null
  columns NULL, so the same INSERT ... RETURNING query raises:

        OneLibraryError: Diesel error: Unexpected null for non-null column

  The crate also exposes :class:`one_library.NewContent` as a Rust
  builder, but it is not instantiable from Python
  (``TypeError: cannot create 'builtins.NewContent' instances``).

Consequence
~~~~~~~~~~~

For sync purposes we **never** need to build a OneLibrary from scratch:
users always have a Rekordbox-produced ``exportLibrary.db`` that we can
copy as a scaffold and then mutate.  This writer therefore takes a
*template* DB as input (typically the user's last export), overlays
playlists + content updates, and writes the result to the target path.

Public API
----------

:func:`write_onelibrary` — copy a template OneLibrary, overlay
tracks+playlists, and write to the target path.  Returns the output
path.

:class:`OneLibraryWriteResult` — summary of the write (tracks written,
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

try:  # pragma: no cover - optional dep; tests skip when missing.
    from apps.sync.usb.pioneer.rbox_runtime import ensure_rbox

    rbox = ensure_rbox()
    OneLibrary = rbox.OneLibrary

    RBOX_AVAILABLE = True
    RBOX_IMPORT_ERROR: str | None = None
except Exception as exc:  # noqa: BLE001 — rbox import is brittle on Windows.
    rbox = None  # type: ignore[assignment]
    OneLibrary = None  # type: ignore[assignment, misc]
    RBOX_AVAILABLE = False
    RBOX_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


__all__ = [
    "OneLibraryWriteError",
    "OneLibraryWriteResult",
    "TrackUpdate",
    "PlaylistSpec",
    "RBOX_AVAILABLE",
    "RBOX_IMPORT_ERROR",
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
    rbox_version: str


def _rbox_version() -> str:
    if not RBOX_AVAILABLE:
        return "unavailable"
    # rbox does not expose ``__version__``; use installed-dist metadata.
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("rbox")
    except PackageNotFoundError:  # pragma: no cover
        return "unknown"
    except Exception:  # pragma: no cover
        return "unknown"


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
        a valid SQLCipher OneLibrary file readable by rbox.
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
        On any of: rbox unavailable; template missing; output exists +
        no overwrite; rbox raises while applying overlays.
    """
    if not RBOX_AVAILABLE:
        raise OneLibraryWriteError(
            f"rbox is not installed ({RBOX_IMPORT_ERROR}). "
            "Install with `pip install rbox`."
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
    # rbox's OneLibrary(path) has no read-only mode: every open may write
    # to the DB and materialise -shm/-wal sidecars next to it. If the
    # caller accidentally points template_path at the committed fixture
    # (or passes the same path as template and output), that will mutate
    # files under tests/fixtures/ on every run. Catch both cases loudly.
    if template == output:
        raise OneLibraryWriteError(
            "template_path must not equal output_path: rbox would open "
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

    try:
        db = OneLibrary(str(output))  # type: ignore[misc]
    except Exception as exc:
        raise OneLibraryWriteError(
            f"Failed to open template copy at {output}: {exc}"
        ) from exc

    tracks_updated = 0
    playlist_ids: list[int] = []

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
            # _RustMutableMapping supports __setitem__ semantics.
            content[key] = value
        try:
            db.update_content(content)
        except Exception as exc:
            raise OneLibraryWriteError(
                f"rbox update_content(id={update.id}) failed: {exc}"
            ) from exc
        tracks_updated += 1

    # --- New playlists -----------------------------------------------
    if playlists:
        # Compute next-available seq after the existing max so we never
        # hit rbox's "Invalid sequence number" uniqueness check.
        existing = db.get_playlists()
        existing_seqs = [int(p["seq"]) for p in existing if p["seq"] is not None]
        next_seq = max(existing_seqs) + 1 if existing_seqs else 0

        for spec in playlists:
            try:
                playlist = db.create_playlist(
                    spec.name, spec.parent_id, next_seq
                )
            except Exception as exc:
                raise OneLibraryWriteError(
                    f"rbox create_playlist(name={spec.name!r}, "
                    f"seq={next_seq}) failed: {exc}"
                ) from exc
            playlist_ids.append(int(playlist["id"]))
            for idx, track_id in enumerate(spec.track_ids):
                try:
                    db.create_playlist_content(
                        int(playlist["id"]), int(track_id), idx
                    )
                except Exception as exc:
                    raise OneLibraryWriteError(
                        f"rbox create_playlist_content(pl={playlist['id']}, "
                        f"content={track_id}, seq={idx}) failed: {exc}"
                    ) from exc
            next_seq += 1

    # --- Flush + size -------------------------------------------------
    # rbox has no explicit commit/close API — dropping the handle
    # releases the SQLite connection in Rust.  Force that by deleting
    # the local binding.
    del db

    size_bytes = output.stat().st_size
    return OneLibraryWriteResult(
        output_path=output,
        tracks_updated=tracks_updated,
        playlists_written=len(playlist_ids),
        playlist_ids=tuple(playlist_ids),
        output_size_bytes=size_bytes,
        rbox_version=_rbox_version(),
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
    if not RBOX_AVAILABLE:
        raise OneLibraryWriteError(
            f"rbox is not installed ({RBOX_IMPORT_ERROR})."
        )
    db = OneLibrary(str(onelibrary_path))  # type: ignore[misc]
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
        # NOTE: rbox's _RustMutableMapping.get() requires an explicit
        # default argument (unlike Python's dict.get). Pass None explicitly.
        "tracks": [
            {
                "id": int(c["id"]),
                "title": c.get("title", None),
                "bpmx100": c.get("bpmx100", None),
                "rating": c.get("rating", None),
                "path": c.get("path", None),
            }
            for c in contents
        ],
    }


__all__.append("read_playlist_roundtrip")
