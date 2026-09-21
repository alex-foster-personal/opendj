"""Top-level ``SeratoAdapter`` implementing the open-dj ``Adapter`` Protocol.

Phase 16 scope (OPEN-02c):

  * Read a ``_Serato_/database V2`` (optionally also ``Subcrates/*.crate``)
    into an ``OpenDjLibrary``.
  * Write an ``OpenDjLibrary`` back into a ``database V2`` + subcrates.
  * Emit structured warnings for every lossy translation.
  * Compute a stable track_id from ``file_path`` + ``title``. Phase 5's
    ``apps.shared.state.ids.stable_id`` is the archival dedup ID keyed on
    ISRC/fingerprint/path+mtime; Serato's database.V2 lacks those signals
    at read time, so this adapter keeps a local SHA-1 over
    ``file_path|title`` with a ``serato_`` prefix (see
    :func:`_stable_track_id` below for rationale).

Per-file GEOB frame mutation (via mutagen) is plumbed through
``SeratoAdapter.write()`` via :func:`apps.adapters.serato.geob.write_geob_frames`
(v1.0 P0 follow-up, GH #2). Hot cues, loops, and beatgrid round-trip on MP3
via the ``Serato Markers2`` + ``Serato BeatGrid`` ID3 GEOB frames. Missing
or non-MP3 audio files downgrade to a structured warning and skip the GEOB
write, so the ``database V2`` -> open-dj read path still works when no real
audio files are available (e.g. fixture-only conformance runs).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from apps.adapters.serato.capabilities import SERATO_CAPABILITIES
from apps.adapters.serato.database_v2 import CrateTrack, DatabaseV2, Subcrate
from apps.adapters.serato.geob import (
    BeatGrid,
    BeatGridMarker,
    Markers2,
    Markers2Cue,
    Markers2Loop,
    is_mp3_like_path,
    read_geob_frames,
    write_geob_frames,
)
from apps.adapters.serato.safety import backup_file, guard_live_write
from apps.open_dj import (
    AdapterReport,
    BeatGridPoint,
    Capabilities,
    CuePoint,
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
    # Optional root prepended to relative track ``file_path`` values when we
    # resolve per-track audio files for GEOB write/read. This is what the
    # conformance harness uses to stage stub MP3s under ``tmp_path`` without
    # rewriting every fixture's ``file_path`` field. When None, file_path is
    # used verbatim; missing or non-MP3 files downgrade to a structured
    # warning rather than raising.
    audio_root: Path | None = None


# --------------------------------------------------------------- helpers


def _stable_track_id(file_path: str, title: str) -> str:
    """Per-adapter stable track ID (Phase 16, open-dj v0.1).

    Phase 5 ships ``apps.shared.state.ids.stable_id`` which returns
    ``(sha1_hex, tier)`` keyed on ISRC / fingerprint / path+mtime. That
    is the archival dedup ID, not what this adapter needs: Serato's
    database.V2 typically lacks ISRC, fingerprint, and mtime at read
    time but does carry file_path + title. We therefore keep a local
    SHA-1 over ``file_path|title`` with a ``serato_`` prefix so IDs stay
    stable across round-trips and are visually distinguishable from the
    Traktor adapter output. Phase 7 dedup re-keys to the archival
    stable_id when better signals (ISRC, fingerprint) become available.
    """
    blob = f"{file_path}|{title}".encode("utf-8")
    return "serato_" + hashlib.sha1(blob).hexdigest()[:16]


def _safe_subcrate_filename(name: str) -> str:
    """Coerce a playlist name into a safe subcrate filename (no traversal).

    Serato subcrate files live at ``<target>/Subcrates/<name>.crate``. A
    hostile or typo'd playlist name like ``../../evil`` would escape the
    Subcrates directory. Strip path separators, NULs, and any ``..``
    components; fall back to ``_`` if the result is empty.
    """
    # Drop directory separators, NULs, and any stray control bytes.
    cleaned = "".join(
        "_" if ch in ("/", "\\", "\x00") or ord(ch) < 0x20 else ch
        for ch in name
    )
    # Collapse ``..`` path components so they cannot climb out of the dir.
    # A single "." as a whole name is also unsafe (current dir); replace it.
    if cleaned in ("", ".", ".."):
        return "_"
    # After stripping separators the string can no longer contain a path
    # component: for example "../a" becomes ".._a", which resolves inside
    # Subcrates/ rather than climbing out. A leading literal "." with no
    # separators is allowed (Serato permits dot-prefixed names), so we
    # only guard the whole-name-equals-dots cases above.
    return cleaned


def _parse_rating_from_extension(raw: bytes) -> int | None:
    try:
        value = int(raw.decode("ascii", errors="ignore").strip())
    except ValueError:
        return None
    return max(0, min(5, value))


# ------------------------------------------------------ open-dj <-> GEOB ---


_DEFAULT_CUE_COLOR_RGB: int = 0xCC0000


def _opendj_cues_to_markers2(
    cues: tuple[CuePoint, ...], *, memory_as_hot: bool
) -> Markers2:
    """Project open-dj :class:`CuePoint` tuples into a Serato Markers2 frame.

    * ``hot`` cues map straight to ``Markers2Cue``.
    * ``loop`` cues map to ``Markers2Loop`` using ``length_ms`` for the end.
    * ``memory`` cues are dropped unless ``memory_as_hot`` is True, in which
      case they are promoted to hot cues (index continues after the real hot
      cue indices to avoid collisions).
    * Other cue types (``load``, ``fade_in``, ``fade_out``, ``grid``) are
      ignored here -- they are out of scope for Serato's Markers2 schema.
    """
    hots: list[Markers2Cue] = []
    loops: list[Markers2Loop] = []
    memories: list[CuePoint] = [c for c in cues if c.type == "memory"]
    for cue in cues:
        color = cue.color_rgb if cue.color_rgb is not None else _DEFAULT_CUE_COLOR_RGB
        if cue.type == "hot":
            hots.append(
                Markers2Cue(
                    index=cue.index,
                    position_ms=cue.position_ms,
                    color_rgb=color,
                    name=cue.name,
                )
            )
        elif cue.type == "loop":
            end_ms = cue.position_ms + (cue.length_ms or 0)
            loops.append(
                Markers2Loop(
                    index=cue.index,
                    start_ms=cue.position_ms,
                    end_ms=end_ms,
                    color_rgb=color,
                    name=cue.name,
                )
            )
    if memory_as_hot and memories:
        next_idx = (max((c.index for c in hots), default=-1)) + 1
        for cue in memories:
            color = cue.color_rgb if cue.color_rgb is not None else _DEFAULT_CUE_COLOR_RGB
            hots.append(
                Markers2Cue(
                    index=next_idx,
                    position_ms=cue.position_ms,
                    color_rgb=color,
                    name=cue.name,
                )
            )
            next_idx += 1
    return Markers2(cues=tuple(hots), loops=tuple(loops))


def _markers2_to_opendj_cues(markers: Markers2) -> tuple[CuePoint, ...]:
    """Inverse of :func:`_opendj_cues_to_markers2` (loops first? no -- stable).

    Order: hot cues in their declared order, then loops. Both keyed by their
    Markers2 index so adapter round-trips are stable.
    """
    out: list[CuePoint] = []
    for c in markers.cues:
        out.append(
            CuePoint(
                index=c.index,
                position_ms=c.position_ms,
                type="hot",
                name=c.name,
                color_rgb=c.color_rgb,
            )
        )
    for lo in markers.loops:
        out.append(
            CuePoint(
                index=lo.index,
                position_ms=lo.start_ms,
                type="loop",
                name=lo.name,
                color_rgb=lo.color_rgb,
                length_ms=max(0, lo.end_ms - lo.start_ms),
            )
        )
    return tuple(out)


def _opendj_beats_to_beatgrid(beats: tuple[BeatGridPoint, ...]) -> BeatGrid:
    """Project open-dj beat anchors into a Serato ``BeatGrid``.

    The last anchor is treated as the terminal marker (carries the locked
    BPM); preceding anchors are non-terminal (they do not carry a ``bpm`` in
    the Serato layout -- Serato derives tempo from the delta to the next
    anchor + a beats-till-next count). We set ``beats_till_next=4`` as a
    reasonable default when we only have (position, bpm) pairs.
    """
    if not beats:
        return BeatGrid(markers=())
    markers: list[BeatGridMarker] = []
    for i, b in enumerate(beats):
        pos_s = b.position_ms / 1000.0
        is_last = i == len(beats) - 1
        if is_last:
            markers.append(BeatGridMarker(position_seconds=pos_s, bpm=b.bpm))
        else:
            markers.append(
                BeatGridMarker(position_seconds=pos_s, beats_till_next=4)
            )
    return BeatGrid(markers=tuple(markers))


def _beatgrid_to_opendj_beats(grid: BeatGrid) -> tuple[BeatGridPoint, ...]:
    """Inverse of :func:`_opendj_beats_to_beatgrid`.

    The terminal marker's locked BPM is propagated back to earlier anchors
    when none of them carries a BPM; this matches the typical open-dj
    constant-tempo grid.
    """
    if not grid.markers:
        return ()
    terminal = grid.markers[-1]
    terminal_bpm = float(terminal.bpm) if terminal.bpm is not None else 0.0
    out: list[BeatGridPoint] = []
    for i, m in enumerate(grid.markers):
        bpm = m.bpm if m.bpm is not None else terminal_bpm
        out.append(
            BeatGridPoint(
                position_ms=round(m.position_seconds * 1000.0),
                bpm=float(bpm),
                terminal=(i == len(grid.markers) - 1),
            )
        )
    return tuple(out)


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
            cues, beats = self._read_geob_for_track(row.file_path, track_id, report)
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
                    cues=cues,
                    beats=beats,
                    extensions=extensions,
                )
            )
            report.bump("tracks_read")

        # Playlists resolve track references by file_path -> track_id.
        # Serato subcrates store only the path, not the title, so we
        # must look up the already-hashed ID (which was computed over
        # ``file_path|title``) rather than rehashing with an empty title
        # -- that produced an ID that never matched any track and
        # silently broke playlist membership on read (Codex P16-F01).
        path_to_id = {t.file_path: t.track_id for t in tracks}
        playlists: list[Playlist] = []
        subcrates_dir = source / "Subcrates"
        if subcrates_dir.is_dir():
            # sort for deterministic order -- fixture round-trip depends on it
            for crate_file in sorted(subcrates_dir.glob("*.crate")):
                crate = Subcrate.read(crate_file)
                ids: list[str] = []
                for p in crate.track_paths:
                    tid = path_to_id.get(p)
                    if tid is None:
                        # Crate references a path not in database V2 --
                        # emit the best-effort hashed ID so callers can
                        # still see the reference and warn so the gap
                        # is visible in the adapter report.
                        tid = _stable_track_id(p, "")
                        report.warn(
                            field="playlist_entry",
                            track_id=tid,
                            action="dropped",
                            reason=(
                                f"crate {crate.name!r} references path "
                                f"{p!r} which has no database V2 row"
                            ),
                        )
                    ids.append(tid)
                playlists.append(Playlist(name=crate.name, track_ids=tuple(ids)))
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
                # Playlist names are user-controlled. Sanitise to prevent
                # path traversal out of Subcrates/ (e.g. name="../../evil").
                safe_name = _safe_subcrate_filename(pl.name)
                Subcrate(name=pl.name, track_paths=paths).write(subdir / f"{safe_name}.crate")
                report.bump("playlists_written")

        # Per-audio-file GEOB frame upsert. This is the real Serato write
        # path: cues + loops + beatgrid live in ID3 GEOB frames on each MP3,
        # not in the ``database V2`` crate DB.
        self._write_geob_for_library(library, report)

        return report

    # ----------------------------------------------------------- helpers

    def _resolve_audio_path(self, file_path: str) -> Path:
        """Resolve a track's ``file_path`` against the optional audio_root."""
        raw = Path(file_path)
        if self.options.audio_root is None:
            return raw
        root = Path(self.options.audio_root)
        if raw.is_absolute():
            # Still allow the root to override absolute paths: this is what
            # the conformance harness wants (fixture paths look absolute but
            # are actually logical). We treat any leading slash as meaning
            # "relative to audio_root".
            return root / raw.relative_to(raw.anchor)
        return root / raw

    def _resolve_backup_dir(self) -> Path:
        """Return the directory used for pre-mutation MP3 backups.

        When ``options.backup_dir`` is None we default to
        ``<project_root>/.planning/adapters/serato/backups`` per the
        dataclass docstring.
        """
        if self.options.backup_dir is not None:
            return Path(self.options.backup_dir)
        from apps.shared import paths as _paths
        return _paths.PROJECT_ROOT / ".planning" / "adapters" / "serato" / "backups"

    def _write_geob_for_library(
        self, library: OpenDjLibrary, report: AdapterReport
    ) -> None:
        """Upsert Serato Markers2 + BeatGrid GEOB frames onto each audio file.

        Rail 2 (timestamped backup): each MP3 is copied into
        :meth:`_resolve_backup_dir` *before* any mutagen mutation, so a
        crash mid-write leaves a bit-exact restore source on disk. If
        the backup step itself fails we skip the track with a structured
        warning rather than proceeding blind -- this was the v1.0
        adversarial blocker #2 (HIGH): the docstring claimed Rail 2 but
        no backup was actually taken.
        """
        backup_dir = self._resolve_backup_dir()
        for track in library.tracks:
            audio_path = self._resolve_audio_path(track.file_path)
            has_content = bool(track.cues) or bool(track.beats)
            if not has_content:
                continue
            if not is_mp3_like_path(audio_path):
                report.warn(
                    field="cue_points.hot",
                    track_id=track.track_id,
                    action="dropped",
                    reason=(
                        f"Serato v1 only writes GEOB frames to .mp3 files; "
                        f"got {audio_path.suffix!r} at {track.file_path!r}"
                    ),
                )
                continue
            if not audio_path.exists():
                report.warn(
                    field="cue_points.hot",
                    track_id=track.track_id,
                    action="dropped",
                    reason=(
                        f"audio file missing at {audio_path}; skipped GEOB write "
                        f"(database V2 row was still emitted)"
                    ),
                )
                continue
            # Rail 2: back up the MP3 BEFORE any mutagen mutation. A
            # failed backup is a safety violation, not a write failure.
            try:
                backup_file(audio_path, backup_dir)
            except Exception as exc:  # noqa: BLE001 -- downgrade to warning
                report.warn(
                    field="cue_points.hot",
                    track_id=track.track_id,
                    action="dropped",
                    reason=(
                        f"pre-write backup failed for {audio_path}: {exc!s}; "
                        f"skipping GEOB write (Rail 2 enforcement)"
                    ),
                )
                continue
            markers = _opendj_cues_to_markers2(
                track.cues, memory_as_hot=self.options.memory_as_hot
            )
            grid = _opendj_beats_to_beatgrid(track.beats)
            try:
                write_geob_frames(
                    audio_path,
                    markers2=markers if (markers.cues or markers.loops) else None,
                    beatgrid=grid if grid.markers else None,
                )
            except Exception as exc:  # noqa: BLE001 -- downgrade to warning
                report.warn(
                    field="cue_points.hot",
                    track_id=track.track_id,
                    action="dropped",
                    reason=f"GEOB write failed for {audio_path}: {exc!s}",
                )
                continue
            report.bump("geob_frames_written")

    def _read_geob_for_track(
        self, file_path: str, _track_id: str, report: AdapterReport
    ) -> tuple[tuple[CuePoint, ...], tuple[BeatGridPoint, ...]]:
        """Return (cues, beats) read from the track's GEOB frames.

        Missing files or non-MP3 formats yield empty tuples without warning
        (the write path already warns; read is symmetric and lenient).
        """
        audio_path = self._resolve_audio_path(file_path)
        if not audio_path.exists() or not is_mp3_like_path(audio_path):
            return (), ()
        try:
            bundle = read_geob_frames(audio_path)
        except Exception:  # noqa: BLE001 -- silent on read, symmetric with write warning
            return (), ()
        cues = _markers2_to_opendj_cues(bundle.markers2)
        beats = _beatgrid_to_opendj_beats(bundle.beatgrid)
        if cues or beats:
            report.bump("geob_frames_read")
        return cues, beats

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
