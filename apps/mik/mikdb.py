"""Read-only reader for MIK's Core Data store (``Collection10.mikdb``).

MIK is READ-ONLY to us as policy: we never write to its store, and
:func:`open_ro` is the only way this package opens one. It uses the
``file:...?mode=ro`` URI plus ``PRAGMA query_only`` so a write attempt raises
at execute time rather than corrupting a 1.37 GiB store we cannot regenerate.

What is read (and why the rest is skipped) is settled in
``.planning/legacy-app-audits/MIK-AUDIT.md``:

* ``ZSONG``          -- scalars: Camelot key, energy 1-10, BPM, loudness.
* ``ZKEYSEGMENT``    -- exactly one row per song, so its ``ZCONFIDENCE`` is a
  per-track KEY CONFIDENCE, not a key timeline. OBSERVED: zero songs have more
  than one row. Do not model it as a series.
* ``ZENERGYSEGMENT`` -- the energy TIME SERIES, the only copy anywhere in the
  toolchain (MIK's own tag write-back is scalar-only).
* ``ZBOOKMARKDATA``  -- the file path, inside a macOS bookmark blob.

Skipped: ``ZCUEPOINT`` (every name NULL, auto energy boundaries not hot cues),
``ZBEATS`` (rekordbox's grid is what the CDJs read), ``ZWAVEFORM`` (88% of the
file, regenerable).

Range/cardinality probe run against all 7,026 rows Tue 28 Jul 2026 (SKILL 4b
step 1, done before any cross-source join):

===================  =======================================================
column               observed
===================  =======================================================
ZSONG.ZENERGY        1.0 .. 9.0, 9 distinct, all integral
ZENERGYSEGMENT.      1.0 .. 10.0, 10 distinct, all integral -> SAME 1-10
  ZENERGY              scale as the scalar
ZSONG.ZTEMPO         0.0 .. 172.0; ONE row is 0.0 and is rejected here
ZSONG.ZVOLUME        -31.2 .. -3.9, mean -11.7. Some dB-ish unit, WHICH one is
                       unproven -> the equivalence gate must clear ``loudness``
                       before it can be written anywhere
ZSONG.               0 .. 88,680, zero NULL. 2,379 tracks (33.9%) carry at
  ZCLIPPEDPEAKCOUNT    least one clipped peak. Free QC data, 100% incremental,
                       and an OBSERVATION MIK made rather than a record of
                       another app's edit
ZSONG.ZKEY           25 distinct: 1A..12B plus a literal ``"0"`` on 3 rows,
                       which is rejected here
ZKEYSEGMENT.         0.0 .. 0.9996, already a [0,1] probability
  ZCONFIDENCE
ZENERGYSEGMENT.      0 rows NULL; 81 rows in -0.0195 .. -0.00014 s, i.e.
  ZSTARTTIME           sub-20ms float artefacts, clamped to 0, COUNTED, and
                       flagged per row via EnergySegment.start_clamped
ZENERGYSEGMENT.      4.2 .. 460.6 s, none <= 0. NOT rounded independently of
  ZLENGTH              the start: see _segment_rows_by_song for why that would
                       corrupt half the corpus
===================  =======================================================
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .bookmark import BookmarkParseError, parse_bookmark_path

log = logging.getLogger(__name__)

DEFAULT_STORE = Path.home() / (
    "Library/Application Support/Mixedinkey/Collection10.mikdb"
)
FLOW_STORE = Path.home() / (
    "Library/Containers/com.mixedinkey.Flow/Data/Library/Application Support/"
    "com.mixedinkey.Flow/Energetic.mikdb"
)

# Core Data stores dates as seconds since 2001-01-01T00:00:00Z.
CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

# MIK prefixes its own ZNAME with the energy digit, e.g. "7 - Control".
ENERGY_PREFIX_MAX_DIGITS = 2

# Sub-frame negative start times are float artefacts, not data. Anything
# further negative than this is a bug we want to see, not paper over.
NEGATIVE_START_TOLERANCE_MS = 1000

ENERGY_MIN, ENERGY_MAX = 1, 10
BPM_MIN, BPM_MAX = 40.0, 220.0


class MikReadError(RuntimeError):
    """The MIK store is not shaped the way this reader requires."""


@dataclass(frozen=True)
class EnergySegment:
    """One energy segment, already converted to milliseconds.

    ``start_clamped`` records that MIK's own start time was NEGATIVE and was
    clamped to 0. It travels into ``track_energy_segments`` as provenance
    rather than living only in a run counter, so a later reader can tell a
    genuine 0 start from a clamped one.
    """

    seq: int
    start_ms: int
    length_ms: int
    energy: int
    start_clamped: bool = False


@dataclass(frozen=True)
class MikSong:
    """One ``ZSONG`` row, cleaned. ``None`` means "MIK has no usable value"."""

    row_id: int
    title: str | None
    artist: str | None
    album: str | None
    path: str | None
    path_error: str | None
    key_camelot: str | None
    key_confidence: float | None
    energy: int | None
    bpm: float | None
    loudness: float | None
    clipped_peak_count: int | None
    analysed_at: str | None
    segments: tuple[EnergySegment, ...] = ()

    @property
    def stripped_title(self) -> str | None:
        """``title`` without MIK's ``"7 - "`` energy prefix."""
        return strip_energy_prefix(self.title)


@dataclass
class ReadStats:
    """Counters for everything the reader refused, so nothing is silent."""

    songs: int = 0
    bookmark_missing: int = 0
    bookmark_unparseable: int = 0
    key_rejected: int = 0
    energy_rejected: int = 0
    bpm_rejected: int = 0
    loudness_rejected: int = 0
    clipped_peak_count_rejected: int = 0
    confidence_rejected: int = 0
    segments_read: int = 0
    segments_rejected: int = 0
    segments_clamped_negative_start: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


# ------------------------------------------------------------- helpers


def open_ro(store_path: Path | None = None) -> sqlite3.Connection:
    """Open a ``.mikdb`` READ-ONLY. The only door into a MIK store."""
    target = Path(store_path) if store_path is not None else DEFAULT_STORE
    if not target.exists():
        raise FileNotFoundError(f"MIK store not found at {target}")
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, isolation_level=None)
    conn.execute("PRAGMA query_only = ON")
    return conn


def core_data_date_to_iso(raw: float | None) -> str | None:
    """Core Data seconds-since-2001 -> RFC 3339 UTC, or None."""
    if raw is None:
        return None
    return (CORE_DATA_EPOCH + timedelta(seconds=float(raw))).isoformat()


def strip_energy_prefix(title: str | None) -> str | None:
    """Strip MIK's leading ``"<digits> - "`` energy prefix from a title.

    Hand-rolled rather than a regex so the digit-count bound is explicit:
    only 1-2 leading digits count, which keeps a real title like
    ``"1979 - Remaster"`` intact.
    """
    if title is None:
        return None
    text = title.strip()
    cut = 0
    while cut < len(text) and text[cut].isdigit():
        cut += 1
    if cut == 0 or cut > ENERGY_PREFIX_MAX_DIGITS:
        return text or None
    rest = text[cut:]
    stripped = rest.lstrip()
    if not stripped.startswith("-"):
        return text or None
    remainder = stripped[1:].strip()
    return remainder or None


def _clean_key(raw: object, stats: ReadStats) -> str | None:
    if not isinstance(raw, str):
        stats.key_rejected += 1
        stats.note("key: not a string")
        return None
    text = raw.strip().upper()
    if not text or text == "0":
        stats.key_rejected += 1
        stats.note("key: MIK placeholder '0' or empty")
        return None
    digits = text[:-1]
    letter = text[-1:]
    if letter not in ("A", "B") or not digits.isdigit() or not 1 <= int(digits) <= 12:
        stats.key_rejected += 1
        stats.note(f"key: not Camelot 1A-12B ({text!r})")
        return None
    return f"{int(digits)}{letter}"


def _clean_energy(raw: object, stats: ReadStats) -> int | None:
    if raw is None:
        stats.energy_rejected += 1
        stats.note("energy: NULL")
        return None
    if not isinstance(raw, (int, float)):
        stats.energy_rejected += 1
        stats.note("energy: not a number")
        return None
    value = float(raw)
    if value != int(value):
        stats.energy_rejected += 1
        stats.note("energy: non-integral")
        return None
    as_int = int(value)
    if not ENERGY_MIN <= as_int <= ENERGY_MAX:
        stats.energy_rejected += 1
        stats.note(f"energy: outside {ENERGY_MIN}-{ENERGY_MAX}")
        return None
    return as_int


def _clean_bpm(raw: object, stats: ReadStats) -> float | None:
    if raw is None:
        stats.bpm_rejected += 1
        stats.note("bpm: NULL")
        return None
    if not isinstance(raw, (int, float)):
        stats.bpm_rejected += 1
        stats.note("bpm: not a number")
        return None
    value = float(raw)
    if not BPM_MIN <= value <= BPM_MAX:
        stats.bpm_rejected += 1
        stats.note(f"bpm: outside {BPM_MIN}-{BPM_MAX}")
        return None
    return value


def _clean_loudness(raw: object, stats: ReadStats) -> float | None:
    if raw is None:
        stats.loudness_rejected += 1
        stats.note("loudness: NULL")
        return None
    if not isinstance(raw, (int, float)):
        stats.loudness_rejected += 1
        stats.note("loudness: not a number")
        return None
    return float(raw)


def _clean_clipped_peaks(raw: object, stats: ReadStats) -> int | None:
    """MIK's own clipped-peak OBSERVATION, 0 .. 88,680 observed."""
    if raw is None:
        stats.clipped_peak_count_rejected += 1
        stats.note("clipped_peak_count: NULL")
        return None
    if not isinstance(raw, (int, float)):
        stats.clipped_peak_count_rejected += 1
        stats.note("clipped_peak_count: not a number")
        return None
    value = int(raw)
    if value < 0:
        stats.clipped_peak_count_rejected += 1
        stats.note("clipped_peak_count: negative")
        return None
    return value


def _clean_confidence(raw: object, stats: ReadStats) -> float | None:
    if raw is None:
        return None
    if not isinstance(raw, (int, float)):
        stats.confidence_rejected += 1
        stats.note("key_confidence: not a number")
        return None
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        stats.confidence_rejected += 1
        stats.note("key_confidence: outside [0,1]")
        return None
    return value


def _validated_segment_row(
    start_s: float, length_s: float, energy: float, stats: ReadStats
) -> tuple[int, int, int, bool] | None:
    """One ZENERGYSEGMENT row, converted to ms and validated. Split out of
    ``_segment_rows_by_song`` to keep it under the complexity ceiling.
    Returns ``None`` if the row was rejected (rejection is already recorded
    on ``stats`` by the time this returns)."""
    # Round BOUNDARIES, never a start and a duration independently.
    # round(a) + round(b) != round(a + b), and rounding ZSTARTTIME and
    # ZLENGTH separately makes 3,545 of 7,019 tracks (50.5%) overlap by up
    # to 1 ms even though MIK has ZERO overlaps in seconds. Measured by the
    # equivalence suite Tue 28 Jul 2026. Half the series would land subtly
    # corrupt and every downstream consumer would inherit it.
    start_seconds = float(start_s)
    start_ms = round(start_seconds * 1000)
    end_ms = round((start_seconds + float(length_s)) * 1000)
    clamped = False
    if start_ms < 0:
        if start_ms < -NEGATIVE_START_TOLERANCE_MS:
            stats.segments_rejected += 1
            stats.note("segment: start more than 1s negative")
            return None
        stats.segments_clamped_negative_start += 1
        clamped = True
        start_ms = 0
    length_ms = end_ms - start_ms
    if length_ms <= 0:
        stats.segments_rejected += 1
        stats.note("segment: length rounds to <= 0 ms")
        return None
    energy_value = float(energy)
    if energy_value != int(energy_value) or not (
        ENERGY_MIN <= int(energy_value) <= ENERGY_MAX
    ):
        stats.segments_rejected += 1
        stats.note(f"segment: energy outside {ENERGY_MIN}-{ENERGY_MAX}")
        return None
    return (start_ms, length_ms, int(energy_value), clamped)


def _segment_rows_by_song(
    conn: sqlite3.Connection, stats: ReadStats
) -> dict[int, list[EnergySegment]]:
    """All ZENERGYSEGMENT rows, converted to ms, grouped and ordered by start."""
    by_song: dict[int, list[tuple[int, int, int, bool]]] = {}
    for song_pk, start_s, length_s, energy in conn.execute(
        "SELECT ZSONG, ZSTARTTIME, ZLENGTH, ZENERGY FROM ZENERGYSEGMENT "
        "ORDER BY ZSONG, ZSTARTTIME"
    ):
        stats.segments_read += 1
        if song_pk is None:
            stats.segments_rejected += 1
            stats.note("segment: NULL song fk")
            continue
        if start_s is None or length_s is None or energy is None:
            stats.segments_rejected += 1
            stats.note("segment: NULL start/length/energy")
            continue
        validated = _validated_segment_row(start_s, length_s, energy, stats)
        if validated is None:
            continue
        by_song.setdefault(int(song_pk), []).append(validated)

    out: dict[int, list[EnergySegment]] = {}
    for song_pk, rows in by_song.items():
        rows.sort(key=lambda r: r[0])
        out[song_pk] = [
            EnergySegment(
                seq=index,
                start_ms=s,
                length_ms=length,
                energy=e,
                start_clamped=was_clamped,
            )
            for index, (s, length, e, was_clamped) in enumerate(rows)
        ]
    return out


def _require_tables(conn: sqlite3.Connection) -> None:
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table in ("ZSONG", "ZENERGYSEGMENT", "ZKEYSEGMENT"):
        if table not in names:
            raise MikReadError(
                f"MIK store has no {table} table; a MIK 11 Core Data migration "
                f"may have reshaped the store (see the SKILL's upgrade-risk "
                f"note). Tables present: {sorted(names)}"
            )


# ---------------------------------------------------------------- read


def read_songs(
    conn: sqlite3.Connection,
    *,
    with_segments: bool = True,
) -> tuple[list[MikSong], ReadStats]:
    """Read every ``ZSONG`` row plus its key confidence and energy series.

    Returns the songs and a :class:`ReadStats` counting every rejection, so a
    caller can print exactly what MIK offered and what we refused.
    """
    _require_tables(conn)
    stats = ReadStats()

    confidence_by_song: dict[int, float | None] = {}
    multi_key: list[int] = []
    for raw_song_pk, confidence in conn.execute(
        "SELECT ZSONG, ZCONFIDENCE FROM ZKEYSEGMENT ORDER BY ZSONG"
    ):
        if raw_song_pk is None:
            continue
        song_pk = int(raw_song_pk)
        if song_pk in confidence_by_song:
            multi_key.append(song_pk)
            continue
        confidence_by_song[song_pk] = _clean_confidence(confidence, stats)
    if multi_key:
        # OBSERVED zero on Collection10; Flow's Energetic.mikdb DOES have
        # multi-segment keys. Refuse rather than silently take the first.
        raise MikReadError(
            f"{len(multi_key)} songs have more than one ZKEYSEGMENT row, so "
            f"ZCONFIDENCE is not a per-track scalar in this store. Key import "
            f"is not defined for a key TIMELINE; first offending song pk "
            f"{multi_key[0]}"
        )

    segments = _segment_rows_by_song(conn, stats) if with_segments else {}

    songs: list[MikSong] = []
    for row in conn.execute(
        "SELECT Z_PK, ZNAME, ZARTIST, ZALBUM, ZKEY, ZENERGY, ZTEMPO, ZVOLUME, "
        "ZCLIPPEDPEAKCOUNT, ZANALYSISDATE, ZBOOKMARKDATA FROM ZSONG ORDER BY Z_PK"
    ):
        (
            row_id,
            title,
            artist,
            album,
            key_raw,
            energy_raw,
            tempo_raw,
            volume_raw,
            clipped_raw,
            analysis_date,
            bookmark,
        ) = row
        stats.songs += 1
        path: str | None = None
        path_error: str | None = None
        if bookmark is None:
            stats.bookmark_missing += 1
            path_error = "no ZBOOKMARKDATA"
        else:
            try:
                path = parse_bookmark_path(bookmark).path
            except BookmarkParseError as exc:
                stats.bookmark_unparseable += 1
                path_error = str(exc)
                stats.note(f"bookmark: {exc}")
        songs.append(
            MikSong(
                row_id=int(row_id),
                title=(title or None),
                artist=(artist or None),
                album=(album or None),
                path=path,
                path_error=path_error,
                key_camelot=_clean_key(key_raw, stats),
                key_confidence=confidence_by_song.get(int(row_id)),
                energy=_clean_energy(energy_raw, stats),
                bpm=_clean_bpm(tempo_raw, stats),
                loudness=_clean_loudness(volume_raw, stats),
                clipped_peak_count=_clean_clipped_peaks(clipped_raw, stats),
                analysed_at=core_data_date_to_iso(analysis_date),
                segments=tuple(segments.get(int(row_id), ())),
            )
        )
    return songs, stats


__all__ = [
    "BPM_MAX",
    "BPM_MIN",
    "CORE_DATA_EPOCH",
    "DEFAULT_STORE",
    "ENERGY_MAX",
    "ENERGY_MIN",
    "FLOW_STORE",
    "EnergySegment",
    "MikReadError",
    "MikSong",
    "ReadStats",
    "core_data_date_to_iso",
    "open_ro",
    "read_songs",
    "strip_energy_prefix",
]
