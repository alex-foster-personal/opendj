"""Read-only source readers + the three-tier matcher that builds pairs.

Both stores are opened with ``file:...?mode=ro``. MIK's store lives under
``~/Library`` and is READ-ONLY to us without exception.

Matching is reported at three tiers, separately, because two of the three are
fuzzy and NOT ONE of MIK's 7,006 stored paths resolves on disk (MIK-AUDIT
section 3). Tier provenance is how a bad match gets diagnosed later.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from apps.mik.bookmark import BookmarkParseError, parse_bookmark_path

_MIK_ENERGY_PREFIX = re.compile(r"^\s*\d{1,2}\s*-\s*")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")

class MalformedEnergySegment(ValueError):
    """A ``ZENERGYSEGMENT`` row is missing a required column.

    Raised by :func:`read_mik_energy_segments` rather than silently dropping
    the row (P1 regression, PR #383 review): the production MIK reader
    (:mod:`apps.mik.mikdb`) REJECTS a segment with a NULL start, length, or
    energy, so a single-source verdict computed here over a comprehension
    that quietly excludes the same rows is auditing a truncated series while
    claiming the source structure was sound. Excluding the row from the
    evidence used to be indistinguishable from the row never existing; this
    makes it a structural failure the gate cannot pass around.
    """


TIERS: tuple[str, ...] = ("exact_path", "basename", "artist_title")
TIER_CONFIDENCE: dict[str, str] = {
    "exact_path": "high",
    "basename": "medium (filenames are not unique)",
    "artist_title": "low (fuzzy)",
}


# ------------------------------------------------------------------ rows


@dataclass(frozen=True)
class RbRow:
    """One ``djmdContent`` row, raw values in rekordbox's own units."""

    content_id: str
    path: str | None
    file_name: str | None
    title: str | None
    artist: str | None
    bpm_raw: int | None  # centi-BPM
    key_raw: str | None  # djmdKey.ScaleName, MIXED notation
    length_raw: int | None  # seconds
    rating: int | None  # 0-5
    energy: float | None = None  # no such column
    loudness: float | None = None  # no such column


@dataclass(frozen=True)
class MikRow:
    """One ``ZSONG`` row plus key confidence and analysed span."""

    pk: int
    path: str | None
    title: str | None
    artist: str | None
    key_camelot: str | None
    energy: float | None
    tempo: float | None
    volume: float | None
    rating: int | None
    key_confidence: float | None
    analysed_span_s: float | None
    clipped_peak_count: int | None = None

    @property
    def file_name(self) -> str | None:
        return Path(self.path).name if self.path else None


@dataclass(frozen=True)
class Pairing:
    """One matched (rekordbox, MIK) pair and the tier that matched it."""

    tier: str
    left: RbRow
    right: MikRow


@dataclass(frozen=True)
class MikEnergySegment:
    """One ``ZENERGYSEGMENT`` row: a span of the energy TIME SERIES.

    Time-indexed in seconds, so no BPM dependency (see
    ``docs/terminology-reference/time-series-vs-scalar.md``). This is a shape
    ``track_fields`` cannot hold: many rows per track, not one.
    """

    song_pk: int
    start_s: float
    length_s: float
    energy: float

    @property
    def end_s(self) -> float:
        return self.start_s + self.length_s


# --------------------------------------------------- bookmark decoding


def decode_bookmark_path(blob: bytes | None) -> str | None:
    """Absolute POSIX path inside an Apple bookmark blob, or None.

    Delegates to :func:`apps.mik.bookmark.parse_bookmark_path`, which follows
    the bookmark TOC's ``0x1004`` path-components array rather than scanning
    raw string records in blob order. The ad-hoc scan this replaced treated
    every string record as a candidate path component regardless of its TOC
    key, so a bookmark's volume-name record (present whenever the track lives
    on a non-boot volume, e.g. ``SLATER``) got appended as a trailing path
    component -- ``/Volumes/SLATER/Music/foo.mp3/SLATER`` instead of
    ``/Volumes/SLATER/Music/foo.mp3``.
    """
    if not blob:
        return None
    try:
        decoded = parse_bookmark_path(bytes(blob))
    except BookmarkParseError:
        return None
    return unicodedata.normalize("NFC", decoded.path)


# ---------------------------------------------------------------- readers


def read_rekordbox(db_path: Path) -> list[RbRow]:
    """Every ``djmdContent`` row with its key name and artist name joined."""
    if not db_path.exists():
        raise FileNotFoundError(
            f"rekordbox working copy not found: {db_path} (expected the "
            f"decrypted data/master.plain.db; see CLAUDE.md)"
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT c.ID, c.FolderPath, c.FileNameL, c.Title, c.BPM, c.Length,
                   c.Rating, k.ScaleName AS key_name, a.Name AS artist_name
              FROM djmdContent c
              LEFT JOIN djmdKey k ON k.ID = c.KeyID
              LEFT JOIN djmdArtist a ON a.ID = c.ArtistID
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        RbRow(
            content_id=str(r["ID"]),
            path=r["FolderPath"],
            file_name=r["FileNameL"],
            title=r["Title"],
            artist=r["artist_name"],
            bpm_raw=r["BPM"],
            key_raw=r["key_name"],
            length_raw=r["Length"],
            rating=r["Rating"],
        )
        for r in rows
    ]


def read_mik(db_path: Path) -> list[MikRow]:
    """Every ``ZSONG`` row, read-only, with bookmark paths decoded."""
    if not db_path.exists():
        raise FileNotFoundError(
            f"MIK store not found: {db_path} (expected "
            f"Collection10.mikdb under ~/Library/Application Support/Mixedinkey)"
        )
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT s.Z_PK, s.ZNAME, s.ZARTIST, s.ZKEY, s.ZENERGY, s.ZTEMPO,
                   s.ZVOLUME, s.ZRATING, s.ZCLIPPEDPEAKCOUNT, s.ZBOOKMARKDATA,
                   k.ZCONFIDENCE AS key_confidence,
                   (SELECT MAX(e.ZSTARTTIME + e.ZLENGTH)
                      FROM ZENERGYSEGMENT e WHERE e.ZSONG = s.Z_PK) AS span_s
              FROM ZSONG s
              LEFT JOIN ZKEYSEGMENT k ON k.ZSONG = s.Z_PK
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        MikRow(
            pk=int(r["Z_PK"]),
            path=decode_bookmark_path(r["ZBOOKMARKDATA"]),
            title=r["ZNAME"],
            artist=r["ZARTIST"],
            key_camelot=r["ZKEY"],
            energy=r["ZENERGY"],
            tempo=r["ZTEMPO"],
            volume=r["ZVOLUME"],
            rating=r["ZRATING"],
            key_confidence=r["key_confidence"],
            analysed_span_s=r["span_s"],
            clipped_peak_count=r["ZCLIPPEDPEAKCOUNT"],
        )
        for r in rows
    ]


def read_mik_energy_segments(db_path: Path) -> list[MikEnergySegment]:
    """Every ``ZENERGYSEGMENT`` row, read-only, ordered per song by start time."""
    if not db_path.exists():
        raise FileNotFoundError(f"MIK store not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT ZSONG, ZSTARTTIME, ZLENGTH, ZENERGY
              FROM ZENERGYSEGMENT
             WHERE ZSONG IS NOT NULL
             ORDER BY ZSONG, ZSTARTTIME
            """
        ).fetchall()
    finally:
        conn.close()
    segments: list[MikEnergySegment] = []
    for song, start, length, energy in rows:
        if start is None or length is None or energy is None:
            missing = [
                name
                for name, value in (
                    ("ZSTARTTIME", start),
                    ("ZLENGTH", length),
                    ("ZENERGY", energy),
                )
                if value is None
            ]
            raise MalformedEnergySegment(
                f"ZENERGYSEGMENT row for song {song} is missing "
                f"{', '.join(missing)}; the production MIK reader rejects "
                f"this row rather than loading a truncated energy series"
            )
        segments.append(
            MikEnergySegment(
                song_pk=int(song),
                start_s=float(start),
                length_s=float(length),
                energy=float(energy),
            )
        )
    return segments


# ---------------------------------------------------------------- matching


def _norm_path(value: str | None) -> str | None:
    if not value:
        return None
    return unicodedata.normalize("NFC", value).casefold()


def _norm_name(value: str | None) -> str | None:
    if not value:
        return None
    text = unicodedata.normalize("NFC", value).casefold()
    text = _MIK_ENERGY_PREFIX.sub("", text)
    text = _NON_ALNUM.sub("", text)
    return text or None


def _artist_title_key(artist: str | None, title: str | None) -> str | None:
    norm_title = _norm_name(title)
    if not norm_title:
        return None
    norm_artist = _norm_name(artist) or ""
    return f"{norm_artist}|{norm_title}"


@dataclass
class MatchReport:
    """Tier-by-tier match counts, reported separately as the skill requires."""

    mik_rows: int
    rb_rows: int
    by_tier: dict[str, int]
    matched_mik: int
    distinct_rb: int
    unmatched_mik: int

    def as_dict(self) -> dict[str, object]:
        return {
            "mik_rows": self.mik_rows,
            "rekordbox_rows": self.rb_rows,
            "by_tier": self.by_tier,
            "tier_confidence": TIER_CONFIDENCE,
            "matched_mik_rows": self.matched_mik,
            "distinct_rekordbox_rows": self.distinct_rb,
            "unmatched_mik_rows": self.unmatched_mik,
            "note": (
                "Tiers are reported separately on purpose: two of three are "
                "fuzzy and no MIK stored path resolves on disk. Never quote a "
                "single headline match rate."
            ),
        }


def _index(rows: Iterable[RbRow], key_fn) -> dict[str, RbRow | None]:
    """Index by key; a key held by MORE THAN ONE row is ambiguous, not first-wins.

    A collision on a fuzzy key (basename, normalized artist/title) is not
    evidence for either row -- it is evidence the key cannot tell them apart.
    Silently keeping whichever row SQLite happened to return first manufactures
    a pairing the data never proved. ``None`` marks a collided key so a lookup
    reads the same as "never seen": :func:`match` already skips a ``None``
    result rather than claiming an rb row, so a collision falls out of
    matching entirely instead of picking a side.
    """
    out: dict[str, RbRow | None] = {}
    for row in rows:
        key = key_fn(row)
        if not key:
            continue
        out[key] = None if key in out else row
    return out


def _resolve_claimants(resolved_mik: set[int], entries: list[MikRow]) -> None:
    """Mark every claimant (winner and losers alike) resolved for a tier, so
    a losing duplicate never reappears and gets paired to an unrelated row
    at a weaker tier."""
    resolved_mik.update(entry.pk for entry in entries)


def _mik_strength(mik: MikRow) -> tuple[float, float, int]:
    """Rank duplicate claimants: higher key confidence, then more analyzed
    span, then lowest pk for a deterministic tie-break. ``None`` reads as the
    weakest possible value on either axis rather than winning by omission.
    """
    confidence = mik.key_confidence if mik.key_confidence is not None else -1.0
    span = mik.analysed_span_s if mik.analysed_span_s is not None else -1.0
    return (-confidence, -span, mik.pk)


def match(
    rb_rows: list[RbRow],
    mik_rows: list[MikRow],
    *,
    allow_fuzzy: bool = True,
) -> tuple[list[Pairing], MatchReport]:
    """Pair MIK songs to rekordbox rows over the three tiers, best tier first.

    Each MIK row is matched at most once, at the highest-confidence tier that
    hits. Each rekordbox row is claimed at most once, so the many-to-one
    collapse the audit warns about is visible as ``distinct_rb``.

    Several MIK rows can resolve to the SAME rekordbox row within one tier
    (measured many-to-one MIK data). ``read_mik()`` gives no ordering
    guarantee, so picking the first one seen in ``mik_rows`` would let an
    arbitrary or stale duplicate decide the pairing. Instead every claimant
    for a given rekordbox row is grouped and the strongest one wins, ranked
    by :func:`_mik_strength` (key confidence, then analyzed span, then pk).
    """
    by_path = _index(rb_rows, lambda r: _norm_path(r.path))
    by_base = _index(
        rb_rows,
        lambda r: _norm_path(r.file_name or (Path(r.path).name if r.path else None)),
    )
    by_artist_title = _index(rb_rows, lambda r: _artist_title_key(r.artist, r.title))

    tiers: list[tuple[str, dict[str, RbRow | None], Callable[[MikRow], str | None]]] = [
        ("exact_path", by_path, lambda m: _norm_path(m.path)),
        ("basename", by_base, lambda m: _norm_path(m.file_name)),
        (
            "artist_title",
            by_artist_title,
            lambda m: _artist_title_key(m.artist, m.title),
        ),
    ]
    if not allow_fuzzy:
        tiers = tiers[:1]

    pairings: list[Pairing] = []
    counts = {name: 0 for name in TIERS}
    claimed_rb: set[str] = set()
    matched_mik: set[int] = set()
    resolved_mik: set[int] = set()

    for tier_name, index, key_fn in tiers:
        claimants: dict[str, tuple[RbRow, list[MikRow]]] = {}
        for mik in mik_rows:
            if mik.pk in resolved_mik:
                continue
            key = key_fn(mik)
            if not key:
                continue
            rb = index.get(key)
            if rb is None:
                continue
            if rb.content_id in claimed_rb:
                # This mik row's strongest hit at this tier already belongs
                # to a claimant from an earlier, stronger tier. Resolve it
                # here rather than letting it fall through to a weaker tier,
                # where it could pair with a wholly unrelated rekordbox row.
                resolved_mik.add(mik.pk)
                continue
            claimants.setdefault(rb.content_id, (rb, []))[1].append(mik)

        for rb, entries in claimants.values():
            winner = min(entries, key=_mik_strength)
            claimed_rb.add(rb.content_id)
            matched_mik.add(winner.pk)
            _resolve_claimants(resolved_mik, entries)
            counts[tier_name] += 1
            pairings.append(Pairing(tier=tier_name, left=rb, right=winner))

    report = MatchReport(
        mik_rows=len(mik_rows),
        rb_rows=len(rb_rows),
        by_tier=counts,
        matched_mik=len(matched_mik),
        distinct_rb=len(claimed_rb),
        unmatched_mik=len(mik_rows) - len(matched_mik),
    )
    return pairings, report


__all__ = [
    "TIERS",
    "TIER_CONFIDENCE",
    "MatchReport",
    "MikEnergySegment",
    "MikRow",
    "Pairing",
    "RbRow",
    "decode_bookmark_path",
    "match",
    "read_mik",
    "read_mik_energy_segments",
    "read_rekordbox",
]
