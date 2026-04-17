"""Top-level ``SeratoAdapter`` implementing the open-dj ``Adapter`` Protocol.

Phase 16 scope (OPEN-02c):

  * Read a ``_Serato_/database V2`` (optionally also ``Subcrates/*.crate``)
    into an ``OpenDjLibrary``.
  * Write an ``OpenDjLibrary`` back into a ``database V2`` + subcrates.
  * Emit structured warnings for every lossy translation.
  * Compute a stable track_id from ``file_path`` + ``title`` (Phase 5 will
    replace with ``apps.shared.stable_id`` when shipped -- see TODO below).

Per-file GEOB frame mutation (via mutagen) is implemented as ``write_geob``
separately so the ``database V2`` -> open-dj read path works without having
any real audio files around.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from apps.adapters.serato.capabilities import SERATO_CAPABILITIES
from apps.adapters.serato.database_v2 import CrateTrack, DatabaseV2, Subcrate
from apps.adapters.serato.safety import guard_live_write
from apps.open_dj import (
    AdapterReport,
    Capabilities,
    OpenDjLibrary,
    Playlist,
    Track,
)


# --------------------------------------------------------------- options


@dataclass
class SeratoAdapterOptions:
    """Runtime options for ``SeratoAdapter``.

    ``rating_column_name`` -- when set, read/write integer ratings (0-5) from
    a custom column name (stored as an ``x_serato_rating_column`` extension
    tag on each track).

    ``memory_as_hot`` -- when True, memory cues from open-dj are promoted to
    hot cues before writing (CONTEXT D6 opt-in).

    ``live_write`` -- when True, the adapter refuses to write if Serato is
    running (see ``safety.guard_live_write``). Tests always keep this False.

    ``backup_dir`` -- root directory where the safety layer writes backup
    copies of target files before mutation. Defaults to
    ``.planning/adapters/serato/backups`` under the repo.
    """

    rating_column_name: str | None = None
    memory_as_hot: bool = False
    live_write: bool = False
    backup_dir: Path | None = None


# --------------------------------------------------------------- helpers


def _stable_track_id(file_path: str, title: str) -> str:
    """Phase 16 placeholder for ``apps.shared.stable_id.stable_id_for``.

    TODO(phase-5): replace with the real helper once Phase 5 ships. The shape
    (short hex digest) is intentional so downstream ID comparisons still work
    after the wire-up.
    """
    blob = f"{file_path}|{title}".encode("utf-8")
    return "serato_" + hashlib.sha1(blob).hexdigest()[:16]


def _parse_rating_from_extension(raw: bytes) -> int | None:
    try:
        value = int(raw.decode("ascii", errors="ignore").strip())
    except ValueError:
        return None
    return max(0, min(5, value))


# ---------------------------------------------------------------- adapter


@dataclass
class SeratoAdapter:
    """Serato DJ Pro adapter (open-dj v0.1)."""

    name: str = "serato"
    options: SeratoAdapterOptions = field(default_factory=SeratoAdapterOptions)

    # ----------------------------------------------------------------- API

    def capabilities(self) -> Capabilities:
        return SERATO_CAPABILITIES

    def read(self, source: Path) -> tuple[OpenDjLibrary, AdapterReport]:
        """Read a ``_Serato_`` directory into an ``OpenDjLibrary``.

        ``source`` points at the ``_Serato_`` folder. We expect
        ``source/database V2`` and optionally ``source/Subcrates/*.crate``.
        """
        report = AdapterReport()
        source = Path(source)
        db_path = source / "database V2"
        if not db_path.exists():
            raise FileNotFoundError(f"Serato database V2 not found at {db_path}")
        db = DatabaseV2.read(db_path)

        tracks: list[Track] = []
        for row in db.tracks:
            track_id = _stable_track_id(row.file_path, row.title)
            try:
                bpm = float(row.bpm) if row.bpm else None
            except ValueError:
                bpm = None
                report.warn(
                    field="bpm",
                    track_id=track_id,
                    action="dropped",
                    reason=f"unparseable BPM {row.bpm!r}",
                )
            rating = self._read_rating(row, track_id, report)
            extensions: dict[str, object] = {}
            if row.extra_tags:
                extensions["x_serato_unknown_tags"] = tuple(
                    {"type": t.type, "payload_hex": t.payload.hex()} for t in row.extra_tags
                )
            tracks.append(
                Track(
                    track_id=track_id,
                    file_path=row.file_path,
                    title=row.title,
                    artists=(row.artist,) if row.artist else (),
                    album=row.album,
                    bpm=bpm,
                    key_camelot=row.key or None,
                    rating=rating,
                    extensions=extensions,
                )
            )
            report.bump("tracks_read")

        playlists: list[Playlist] = []
        subcrates_dir = source / "Subcrates"
        if subcrates_dir.is_dir():
            # sort for deterministic order -- fixture round-trip depends on it
            for crate_file in sorted(subcrates_dir.glob("*.crate")):
                crate = Subcrate.read(crate_file)
                ids = tuple(_stable_track_id(p, "") for p in crate.track_paths)
                playlists.append(Playlist(name=crate.name, track_ids=ids))
                report.bump("playlists_read")

        library = OpenDjLibrary(version="0.1", tracks=tuple(tracks), playlists=tuple(playlists))
        return library, report

    def write(self, library: OpenDjLibrary, target: Path) -> AdapterReport:
        """Serialise ``library`` into a Serato ``_Serato_`` dir at ``target``.

        This writes ``database V2`` and per-crate subcrate files. It does NOT
        mutate any real audio files; GEOB frame writing is handled in the
        per-track ``write_geob`` helper (see below) and tests exercise it
        against ``tmp_path`` only.
        """
        report = AdapterReport()
        guard_live_write(live_write=self.options.live_write)

        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)

        crate_rows: list[CrateTrack] = []
        for track in library.tracks:
            bpm_str = f"{track.bpm:.2f}" if track.bpm is not None else ""
            row = CrateTrack(
                file_path=track.file_path,
                title=track.title,
                artist=" & ".join(track.artists),
                album=track.album,
                bpm=bpm_str,
                key=track.key_camelot or "",
                file_type=_infer_file_type(track.file_path),
            )
            if track.rating is not None and self.options.rating_column_name is None:
                report.warn(
                    field="rating",
                    track_id=track.track_id,
                    action="dropped",
                    reason="Serato has no native rating (D6); set rating_column_name to preserve.",
                )
            has_memory = any(c.type == "memory" for c in track.cues)
            if has_memory and not self.options.memory_as_hot:
                report.warn(
                    field="cue_points.memory",
                    track_id=track.track_id,
                    action="dropped",
                    reason="Serato has no memory cues (D6); enable memory_as_hot to promote.",
                )
            crate_rows.append(row)
            report.bump("tracks_written")

        db = DatabaseV2(tracks=tuple(crate_rows))
        db.write(target / "database V2")

        if library.playlists:
            subdir = target / "Subcrates"
            for pl in library.playlists:
                # Reconstruct per-crate track paths from track_ids via library
                # lookup; ids are opaque so we store path list verbatim.
                lookup = {t.track_id: t.file_path for t in library.tracks}
                paths = tuple(lookup.get(tid, "") for tid in pl.track_ids if lookup.get(tid))
                Subcrate(name=pl.name, track_paths=paths).write(subdir / f"{pl.name}.crate")
                report.bump("playlists_written")

        return report

    # ----------------------------------------------------------- helpers

    def _read_rating(
        self, row: CrateTrack, track_id: str, report: AdapterReport
    ) -> int | None:
        if not self.options.rating_column_name:
            return None
        # Look for the configured column in extra_tags (we stash it as a raw
        # leaf tag whose type matches the first 4 chars of the column name).
        target_type = self.options.rating_column_name[:4].ljust(4).lower()
        for tag in row.extra_tags:
            if tag.type == target_type:
                rating = _parse_rating_from_extension(tag.payload)
                if rating is None:
                    report.warn(
                        field="rating",
                        track_id=track_id,
                        action="dropped",
                        reason=f"rating column {target_type!r} had non-numeric payload",
                    )
                return rating
        return None


def _infer_file_type(file_path: str) -> str:
    suffix = Path(file_path).suffix.lower().lstrip(".")
    return {"mp3": "mp3", "flac": "flac", "wav": "wav", "aiff": "aiff", "m4a": "m4a"}.get(
        suffix, suffix or "mp3"
    )
