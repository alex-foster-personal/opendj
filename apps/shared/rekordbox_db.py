"""Thin typed wrapper around :mod:`pyrekordbox` for Rekordbox 6/7 master.db.

We do not re-implement any ORM — we just expose a small set of dataclasses
(:class:`RBTrack`, :class:`RBPlaylist`) and iterator helpers that hide the
rough edges (nullable relationships, BPM×100 storage, streaming FolderPaths).

This module also owns the one SQLCipher decrypt routine in the repo
(:func:`decrypt_to_plain` / :func:`ensure_plain_db`): the live ``master.db``
and its ``data/master.db.copy`` snapshot are encrypted, while every reader
here consumes the decrypted ``data/master.plain.db``.
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import paths, platform_paths

if TYPE_CHECKING:
    from pyrekordbox import Rekordbox6Database


class RekordboxDecryptError(RuntimeError):
    """Raised when SQLCipher decryption of a Rekordbox master.db fails."""


SQLITE_MAGIC: bytes = b"SQLite format 3\x00"


def is_plain_sqlite(path: Path) -> bool:
    """True iff ``path`` opens as an unencrypted SQLite file.

    Header-only, 16 bytes read. An encrypted Rekordbox DB has ciphertext
    where the magic belongs, so this separates "open it directly" from
    "hand it to SQLCipher" on the file's own evidence rather than on its
    name or on where it was found.

    This lives beside the decrypt routine because the two answer the same
    question from opposite sides, and because it is the lowest layer that
    needs it: ``apps.engine_core.setup.detect`` and
    ``apps.reconcile.remove_track`` both import it from here, so the header
    check exists once.
    """
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


# ----- SQLCipher decrypt (single source of truth) ------------------------
#
# Callers: ``apps.shared.state.ingest.rekordbox.run_cli`` (the ingest-rb CLI)
# and ``scripts/make_rb_fixture.py``. Both go through here so the ATTACH +
# ``sqlcipher_export`` incantation exists exactly once.


def decrypt_to_plain(
    src_encrypted: Path, plain_out: Path, *, key: str = ""
) -> Path:
    """Decrypt ``src_encrypted`` to plain SQLite at ``plain_out``.

    Opens the source through pyrekordbox (which applies the SQLCipher key),
    then ATTACHes a brand-new keyless DB and copies every table across with
    ``sqlcipher_export``. Faster than a schema+row rebuild and preserves the
    schema exactly.

    Writes to a sibling temp file and renames on success, so a failed
    decrypt never leaves a half-written ``master.plain.db`` that later runs
    would mistake for a good one.

    Parameters
    ----------
    key
        SQLCipher key. Empty (the default) lets pyrekordbox supply its own,
        which is what every production call site wants.

    Raises
    ------
    FileNotFoundError
        ``src_encrypted`` does not exist.
    RekordboxDecryptError
        The key is unavailable/wrong or the source is not a SQLCipher DB.
    """
    src_encrypted = Path(src_encrypted)
    plain_out = Path(plain_out)
    if not src_encrypted.exists():
        raise FileNotFoundError(
            f"Encrypted Rekordbox DB not found at {src_encrypted}"
        )

    plain_out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = plain_out.with_name(plain_out.name + ".decrypting")
    _unlink_db(tmp_out)

    # Deferred (STANDALONE-01, issue #3535/#3456): a module-scope pyrekordbox
    # import here drags the whole vendor tree into every caller of this
    # module, including ones (the folder-import onboarding path, app
    # startup) that never touch a real rekordbox library. Only the two
    # functions that actually construct a live DB need it at runtime; every
    # other use in this file is a type annotation, made lazy by the
    # ``from __future__ import annotations`` at the top.
    from pyrekordbox import Rekordbox6Database

    db = None
    try:
        db = Rekordbox6Database(path=str(src_encrypted), key=key)
        con = db.engine.raw_connection()
        cur = con.cursor()
        # Quote the literal path defensively; ATTACH takes no bind params
        # for the KEY clause in every sqlcipher build we support.
        escaped = str(tmp_out).replace("'", "''")
        cur.execute(f"ATTACH DATABASE '{escaped}' AS plain KEY ''")
        cur.execute("SELECT sqlcipher_export('plain')")
        cur.execute("DETACH DATABASE plain")
    except Exception as exc:
        _unlink_db(tmp_out)
        raise RekordboxDecryptError(
            f"failed to decrypt {src_encrypted} to {plain_out}: {exc}"
        ) from exc
    finally:
        if db is not None:
            # A close failure here must not mask a successful decrypt, nor
            # shadow the RekordboxDecryptError raised above.
            with contextlib.suppress(Exception):
                db.close()

    _unlink_db(plain_out)
    os.replace(tmp_out, plain_out)
    return plain_out


def ensure_plain_db(
    *,
    encrypted: Path | None = None,
    plain: Path | None = None,
    refresh: bool = False,
    key: str = "",
) -> tuple[Path, bool]:
    """Guarantee a decrypted working copy exists; return ``(path, decrypted)``.

    ``decrypted`` is True when this call did the decryption, False when an
    existing ``plain`` copy was reused. Reuse is the documented convention:
    ``data/master.plain.db`` is static, and refreshing it means re-decrypting
    on purpose (``refresh=True``) plus clearing ``data/state/anlz-cache/``.

    Raises
    ------
    FileNotFoundError
        Neither a usable plain copy nor the encrypted snapshot exists. The
        message names the command that produces the snapshot.
    RekordboxDecryptError
        The snapshot exists but could not be decrypted.
    """
    src = Path(encrypted) if encrypted is not None else paths.REKORDBOX_WORKING_DB
    out = Path(plain) if plain is not None else paths.REKORDBOX_PLAIN_DB

    if out.exists() and not refresh:
        return out, False

    if not src.exists():
        raise FileNotFoundError(
            f"No decrypted Rekordbox DB at {out} and no encrypted snapshot at "
            f"{src}. Snapshot the live master.db first with "
            f"`python -m apps.audit.rekordbox_vs_music` (it calls "
            f"apps.shared.paths.copy_live_dbs), then rerun."
        )

    return decrypt_to_plain(src, out, key=key), True


def _unlink_db(path: Path) -> None:
    """Remove ``path`` plus any SQLite sidecar files, ignoring absences."""
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)


def is_streaming_path(p: str | None) -> bool:
    """True if ``p`` is empty/None or looks like a streaming service URI.

    Alias of :func:`apps.shared.platform_paths.is_unplayable_path`, which is
    the one definition (T3b map D1). Kept under this name because it is
    exported here and imported by reconcile/relocate/sync call sites.
    """
    return platform_paths.is_unplayable_path(p)


@dataclass(slots=True)
class RBTrack:
    id: str
    title: str
    artist: str
    album: str
    genre: str
    folder_path: str  # raw FolderPath -- may be absolute path, URI, or empty
    file_path: Path | None  # None for streaming / empty
    is_streaming: bool
    bpm: float | None
    rating: int | None
    file_size: int | None
    date_added: str | None
    # Phase 15 widening: ISRC (tier-1 stable_id seed, Phase 9 spotify match)
    # and duration in seconds (Phase 2 matcher signal #4). Optional with
    # defaults so existing RBTrack(...) callsites stay backward-compatible.
    isrc: str | None = None
    duration_s: float | None = None
    # DjmdContent.updated_at (StatsFull mixin, onupdate=datetime.now on the
    # live pyrekordbox row) -- the row's own last-modified stamp, used as
    # the export adapter's provenance ``modified_at`` source (OPEN-02).
    # Optional so callers that don't care about provenance timestamps
    # (e.g. apps.reconcile.heal_icloud_paths) can keep constructing
    # RBTrack without it.
    updated_at: datetime | None = None


@dataclass(slots=True)
class RBPlaylist:
    id: str
    name: str
    parent_id: str | None
    track_ids: list[str]


def open_db(path: Path | None = None) -> Rekordbox6Database:
    """Open the working-copy Rekordbox DB. Copies from live if missing.

    Parameters
    ----------
    path
        Optional override. Defaults to :data:`paths.REKORDBOX_WORKING_DB`.

    Raises
    ------
    FileNotFoundError
        If neither the working copy nor the live DB can be found.
    """
    from pyrekordbox import Rekordbox6Database

    target = Path(path) if path is not None else paths.REKORDBOX_WORKING_DB
    if not target.exists():
        copied = paths.copy_live_dbs()
        if copied.get("rekordbox") is None:
            raise FileNotFoundError(
                f"Rekordbox working DB not at {target} and live DB "
                f"{paths.REKORDBOX_LIVE_DB} is missing."
            )
        target = copied["rekordbox"]  # type: ignore[assignment]
    # pyrekordbox defaults ``unlock`` to True, which routes every open through
    # the SQLCipher dialect and applies the Rekordbox key. Handed an already
    # plain file that fails with "file is not a database", so the decision has
    # to come from the file rather than from a default. The normal path is
    # still encrypted: ``paths.copy_live_dbs()`` is a byte copy of the live DB,
    # so ``master.db.copy`` keeps going through SQLCipher exactly as before.
    return Rekordbox6Database(path=str(target), unlock=not is_plain_sqlite(target))


def _safe_name(rel) -> str:
    """Return ``rel.Name`` for a relationship object, else ''."""
    if rel is None:
        return ""
    name = getattr(rel, "Name", None)
    return name or ""


def _to_path(folder_path: str, streaming: bool) -> Path | None:
    if streaming or not folder_path:
        return None
    return Path(folder_path).expanduser()


def _coerce_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date_to_str(value) -> str | None:
    if value is None:
        return None
    # pyrekordbox returns datetime objects for DateCreated; stringify safely.
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def iter_tracks(db: Rekordbox6Database) -> Iterator[RBTrack]:
    """Yield every :class:`RBTrack` row in the DB."""
    for t in db.get_content():
        folder_path = t.FolderPath or ""
        streaming = is_streaming_path(folder_path)

        # BPM is stored as BPM*100 integer. Anything else -> None.
        bpm_raw = _coerce_int(t.BPM)
        bpm = bpm_raw / 100.0 if bpm_raw else None

        # DjmdContent.Length is stored in WHOLE SECONDS (verified against live
        # data: sound-effect samples read Length=5/7, full tracks Length=491
        # for an 8:11 / 15.8 MB @256 kbps file). The previous code divided by
        # 1000 (treating it as ms), making every duration 1000x too small and
        # silently killing the matcher/relocator ``duration_match`` signal.
        # Fall back to None when missing.
        length_s = _coerce_int(getattr(t, "Length", None))
        duration_s = float(length_s) if length_s is not None else None

        isrc_raw = getattr(t, "ISRC", None)
        isrc = isrc_raw.strip() if isinstance(isrc_raw, str) and isrc_raw.strip() else None

        yield RBTrack(
            id=str(t.ID),
            title=t.Title or "",
            artist=_safe_name(t.Artist),
            album=_safe_name(t.Album),
            genre=_safe_name(t.Genre),
            folder_path=folder_path,
            file_path=_to_path(folder_path, streaming),
            is_streaming=streaming,
            bpm=bpm,
            rating=_coerce_int(t.Rating),
            file_size=_coerce_int(t.FileSize),
            date_added=_date_to_str(t.DateCreated),
            isrc=isrc,
            duration_s=duration_s,
            # StatsFull.updated_at is NOT NULL on every real DjmdContent
            # row (pyrekordbox default=datetime.now, onupdate=datetime.now).
            # ``getattr`` (matching the ``Length``/``ISRC`` columns above)
            # because this function also accepts lightweight test doubles
            # that don't model every column -- RBTrack.updated_at is
            # Optional for exactly that reason. A track that reaches the
            # open-dj export adapter without one and needs it (bpm/key/
            # rating set) fails loudly there instead (RuntimeError), never
            # silently here.
            updated_at=getattr(t, "updated_at", None),
        )


def iter_playlists(db: Rekordbox6Database) -> Iterator[RBPlaylist]:
    """Yield every :class:`RBPlaylist`. Track order respects ``TrackNo``."""
    for p in db.get_playlist():
        songs = list(getattr(p, "Songs", []) or [])
        songs.sort(key=lambda s: (getattr(s, "TrackNo", 0) or 0))
        track_ids = [str(s.ContentID) for s in songs if getattr(s, "ContentID", None) is not None]

        parent_id = getattr(p, "ParentID", None)
        # Rekordbox uses the literal string "root" for the top-level parent;
        # normalize that to None so callers can treat top-level as a root.
        if parent_id in (None, "", "root"):
            parent_norm: str | None = None
        else:
            parent_norm = str(parent_id)

        yield RBPlaylist(
            id=str(p.ID),
            name=p.Name or "",
            parent_id=parent_norm,
            track_ids=track_ids,
        )


# ----- Phase 4: cue + analysis readers ---------------------------------


def _rb_kind_to_normalised(kind: int | None, index_hint: int | None) -> tuple[str, int | None]:
    """Map pyrekordbox ``DjmdCue.Kind`` to a ``NormalisedCue`` kind/index.

    Kind semantics (pyrekordbox ``db6/tables.py`` DjmdCue):
      * 0 = memory cue.
      * 1..8 = hot cue slot (1-indexed in RB -> we surface 0..7).
      * Some builds use 3 for load cue and 4 for loop; treat 4 as loop
        when ``ActiveLoop`` is true, otherwise fold into hot.
    """
    k = _coerce_int(kind)
    if k is None or k == 0:
        return "memory", None
    if 1 <= k <= 8:
        return "hot", (index_hint if index_hint is not None else k - 1)
    if k == 4:
        return "loop", None
    return "hot", index_hint


def iter_cues(db: Rekordbox6Database, content_id: str) -> list:
    """Return the normalised cue list for a RB track by ContentID."""
    from .normalised import NormalisedCue
    from .rb_color_palette import color_index_to_rgb

    cues: list = []
    # Prefer the ORM's filtered query; fall back to scanning all rows.
    rows = []
    try:
        rows = list(db.get_cue(ContentID=str(content_id)))
    except Exception:
        try:
            rows = [r for r in db.get_cue() if str(getattr(r, "ContentID", "")) == str(content_id)]
        except Exception:
            rows = []
    for row in rows:
        in_msec = _coerce_int(getattr(row, "InMsec", None))
        if in_msec is None:
            continue
        kind = getattr(row, "Kind", None)
        color_idx = _coerce_int(getattr(row, "Color", None))
        active_loop = bool(getattr(row, "ActiveLoop", False))
        out_msec = _coerce_int(getattr(row, "OutMsec", None))
        comment = getattr(row, "Comment", None) or None
        # pyrekordbox doesn't expose an explicit hot-cue slot column; some
        # builds use ``Kind - 1`` as the slot. Fall back to enumeration order.
        kind_str, index = _rb_kind_to_normalised(kind, None)
        if (kind_str == "hot" and index is None):
            index = len([c for c in cues if c.kind == "hot"])
        if kind_str == "hot" and index is not None and index > 7:
            index = None
        if active_loop and out_msec and out_msec > in_msec:
            kind_str = "loop"
            index = None
        loop_length = (out_msec - in_msec) if (kind_str == "loop" and out_msec) else None
        cue = NormalisedCue(
            position_msec=int(in_msec),
            kind=kind_str,  # type: ignore[arg-type]
            index=index,
            color_rgb=color_index_to_rgb(color_idx),
            name=str(comment) if comment else None,
            loop_length_msec=loop_length,
        )
        cues.append(cue)
    cues.sort(
        key=lambda c: (
            c.position_msec,
            c.kind,
            c.index if c.index is not None else -1,
        )
    )
    return cues


def iter_analysis(db: Rekordbox6Database) -> "Iterator":
    """Yield ``NormalisedAnalysis`` for every RB track.

    BPM is read from ``DjmdContent.BPM`` (stored as BPM×100). Key is
    resolved through the harmonic module's Camelot mapping. Energy uses
    ``ColorID`` as the one-to-one proxy (see 04-RESEARCH §1). Write-back
    converts BPM only at ``djmdContent.BPM``; PQTZ tempo is a separate x100
    at the ANLZ tag (``analysis_writeback_pqtz``). Key is a ``KeyID`` foreign
    key, never a scale name on ``djmdContent``; loudness has no rekordbox column.
    """
    from .harmonic import key_to_camelot
    from .normalised import NormalisedAnalysis

    for t in db.get_content():
        bpm_raw = _coerce_int(t.BPM)
        bpm = bpm_raw / 100.0 if bpm_raw else None
        color_id = _coerce_int(getattr(t, "ColorID", None))
        key_name = _safe_name(getattr(t, "Key", None))
        camelot = None
        if key_name:
            try:
                camelot = str(key_to_camelot(key_name))
            except ValueError:
                camelot = None
        yield NormalisedAnalysis(
            uuid_or_id=str(t.ID),
            source="rb",
            bpm=bpm,
            manual_bpm=None,
            key_camelot=camelot,
            energy=color_id,
            tags=None,
            is_straight_grid=None,
        )


__all__ = [
    "RBTrack",
    "RBPlaylist",
    "RekordboxDecryptError",
    "decrypt_to_plain",
    "ensure_plain_db",
    "open_db",
    "iter_tracks",
    "iter_playlists",
    "iter_cues",
    "iter_analysis",
    "is_streaming_path",
]
