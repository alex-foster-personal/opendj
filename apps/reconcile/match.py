"""Candidate matcher + row classifier for state-layer link repair.

Entry point::

    python -m apps.reconcile.match --data-dir /Users/user/code/music-dj-tools/data
    python -m apps.reconcile.match --limit 200          # quick sample run
    python -m apps.reconcile.match --explain <stable_id>

THIS MODULE NEVER WRITES TO ``state.db``. It opens the state DB through
:func:`apps.shared.state.db.open_ro` (``mode=ro`` URI plus ``PRAGMA
query_only``), so an ``UPDATE`` raises at execute time rather than silently
landing. It never deletes, moves, renames or re-encodes an audio file and
never removes a track row. The only thing it produces is a set of report
artefacts under ``<data-dir>/reconcile/``:

* ``link-repair-classification.csv`` -- one line per track row: bucket, tier,
  confidence, recorded path, best candidate, reason.
* ``link-repair-plan.json``          -- the auto-applicable subset only, each
  entry carrying BOTH ``old_path`` and ``new_path`` so the eventual writer is
  reversible from this file alone.
* ``link-repair-review.csv``         -- ambiguous rows, one line per candidate,
  for human adjudication. Never auto-applied.

The apply step (a separate, future module) is the thing that carries the
``--live`` gate; there is deliberately no ``--live`` flag here, because a flag
that cannot mutate anything would be a lie about this module's blast radius.

Buckets
-------
Every row lands in exactly one bucket (checked by the sum invariant in
``tests/reconcile/test_link_repair.py``), evaluated in this order:

1. ``present``              recorded path resolves on disk right now.
2. ``streaming``            recorded "path" is a service URI (``spotify:``,
   ``soundcloud:``, ``tidal:``, ``http(s)://``). Not a broken file link at all
   -- see the note below.
3. ``malformed-path``       NULL/blank, non-absolute, or a known-bad synthetic
   prefix (``/contents_<digits>/...``, a USB-export artefact).
4. ``awaiting-volume``      under ``/Volumes/<name>`` where ``<name>`` is not
   currently mounted. NOT a dead path: the file is presumed fine on an
   offline drive, so it is never relinked and never written off.
5. ``relinkable-auto``      exactly one candidate at or above
   :data:`AUTO_APPLY_THRESHOLD`, on a file no other row already resolves to,
   and not contested by another auto row.
6. ``relinkable-ambiguous`` everything else that has candidates. ``ambiguity``
   records which of the four reasons applies: ``multiple-above-threshold``
   (2+ strong candidates), ``below-auto-threshold`` (only weak evidence),
   ``target-already-linked`` (the target is another row's resolving file), or
   ``target-contested`` (2+ auto rows want the same file). All need a human.
7. ``absent-no-audio``      no candidate anywhere under the indexed roots.

``streaming`` is a SEVENTH bucket added beyond the six originally specified.
Service URIs identify non-local tracks (modeled as ``is_streaming`` in
:mod:`apps.shared.rekordbox_db`); filing them under ``malformed-path`` would
misclassify a valid non-local source.

Requirements (mini-PRD)
-----------------------
1. Scored candidates per broken row via six ordered tiers, recording which
   tier fired.  OK
   - [if] exactly one disk file shares the basename [then] tier
     ``basename-exact-unique`` fires and no later tier is consulted.
   - [if] three disk files share the basename and one is within 2s of the
     recorded duration [then] tier ``basename-duration`` fires with one
     candidate.
   - [if] the recorded ISRC matches a tagged file but the basename differs
     [then] tier ``isrc`` fires.
2. Ambiguity is never auto-applied.  OK
   - [if] 2+ candidates score at/above the threshold [then] bucket is
     ``relinkable-ambiguous`` and the plan JSON excludes the row.
   - [if] the only candidate scores below the threshold [then] bucket is
     ``relinkable-ambiguous`` with ``ambiguity=below-auto-threshold``.
   - [if] the sole strong candidate is already another row's resolving file
     [then] ``ambiguity=target-already-linked`` and it stays out of the plan.
   - [if] two rows each have that same sole strong candidate [then] both are
     demoted with ``ambiguity=target-contested``.
3. Buckets are mutually exclusive and sum to the row total.  OK
   - [if] bucket counts do not sum to ``select count(*) from tracks`` [then]
     the classifier raises rather than reporting a plausible-looking lie.
4. Unmounted volumes are never treated as dead.  OK
   - [if] a row points at ``/Volumes/<name>/...`` and that volume is unmounted
     [then] bucket is ``awaiting-volume`` and candidates are not computed.

Regression contract: ``tests/reconcile/test_link_repair.py``.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from rich.console import Console
from rich.table import Table

from apps.reconcile.index_disk import (
    CACHE_PATH,
    DEFAULT_ROOTS,
    DiskAudio,
    DiskIndex,
    basename_key,
    build_index,
    normalise_isrc,
    normalise_text,
)
from apps.shared import paths
from apps.shared.sandbox import refuse_if_sandboxed
from apps.shared.state import db as state_db_mod

console = Console(width=120)

# ----- tiers -------------------------------------------------------------

Tier = Literal[
    "basename-exact-unique",
    "basename-duration",
    "basename-size",
    "isrc",
    "title-artist-fuzzy",
    "duration-title-fuzzy",
]

# Evaluation order is exactly the order specified for this feature. The first
# tier that yields at least one candidate wins and later tiers are skipped, so
# the recorded tier is always the strongest evidence available for that row.
TIER_ORDER: tuple[Tier, ...] = (
    "basename-exact-unique",
    "basename-duration",
    "basename-size",
    "isrc",
    "title-artist-fuzzy",
    "duration-title-fuzzy",
)

# A candidate at or above this confidence, and alone in that, may be applied
# without a human. Everything below lands in review.
AUTO_APPLY_THRESHOLD: float = 0.85

CONF_BASENAME_UNIQUE: float = 0.95
CONF_BASENAME_DURATION: float = 0.90
CONF_BASENAME_SIZE: float = 0.88
CONF_ISRC: float = 0.92

# Fuzzy tiers are capped BELOW the auto-apply threshold on purpose: text
# similarity alone has never been strong enough to repoint a real library
# without eyes on it.
CONF_TITLE_ARTIST_BASE: float = 0.60
CONF_TITLE_ARTIST_SPAN: float = 0.20
CONF_DURATION_TITLE_BASE: float = 0.45
CONF_DURATION_TITLE_SPAN: float = 0.20

DURATION_TOLERANCE_S: float = 2.0
TITLE_SIMILARITY_MIN: float = 0.90  # tier e, title component
ARTIST_SIMILARITY_MIN: float = 0.85  # tier e, artist component
TITLE_ONLY_SIMILARITY_MIN: float = 0.85  # tier f, title vs tag-or-filename

# Blocking limits keep fuzzy-tier candidate work bounded.
# A token appearing in more than MAX_TOKEN_POSTINGS files carries
# no discriminating power (think "remix", "mix").
MAX_TOKEN_POSTINGS: int = 300
MAX_FUZZY_CANDIDATES: int = 800

# ----- buckets -----------------------------------------------------------

Bucket = Literal[
    "present",
    "relinkable-auto",
    "relinkable-ambiguous",
    "awaiting-volume",
    "malformed-path",
    "absent-no-audio",
    "streaming",
]

ALL_BUCKETS: tuple[Bucket, ...] = (
    "present",
    "relinkable-auto",
    "relinkable-ambiguous",
    "awaiting-volume",
    "malformed-path",
    "absent-no-audio",
    "streaming",
)

Ambiguity = Literal[
    "multiple-above-threshold",
    "below-auto-threshold",
    "target-already-linked",
    "target-contested",
]

ALL_AMBIGUITIES: tuple[Ambiguity, ...] = (
    "multiple-above-threshold",
    "below-auto-threshold",
    "target-already-linked",
    "target-contested",
)

_STREAMING_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)
_SYNTHETIC_CONTENTS = re.compile(r"^/contents_\d+/")
_VOLUME_PATH = re.compile(r"^/Volumes/([^/]+)")


def mounted_volume_names() -> frozenset[str]:
    """Names currently present under ``/Volumes`` (Macintosh HD included).

    Computed at runtime, never cached across a run's lifetime longer than one
    classification pass: a drive plugged in mid-run must not be mistaken for
    an offline one on the next row.
    """
    # SAND-02. A sandboxed process cannot LIST /Volumes, and the failure is
    # silent: is_dir() is False or iterdir() raises, and both fall through to
    # the empty set below. Empty means "every external drive is offline", so
    # every row on a USB drive would be reclassified as awaiting-volume and a
    # user would be told their library had gone missing. Refuse first.
    # No ``instead``: there is no way to do this from inside a store build
    # today, and the two candidates are both wrong to ship. Naming the direct
    # download is the guideline 3.2.2(vi) pattern; naming the file picker
    # would advertise SAND-03, which is not built. See
    # apps.shared.sandbox.store_build_refusal_message.
    refuse_if_sandboxed(
        "Scanning attached drives",
        because="the sandbox does not permit listing /Volumes at all, so "
        "this build cannot see attached drives whether or not any are "
        "plugged in.",
    )
    root = Path("/Volumes")
    if not root.is_dir():
        return frozenset()
    try:
        return frozenset(p.name for p in root.iterdir())
    except OSError:
        return frozenset()


def unmounted_volume_of(path: str | None, mounted: frozenset[str]) -> str | None:
    """Volume name in ``path`` that is NOT currently mounted, else ``None``.

    Public because the apply/undo side (:mod:`apps.reconcile.relink`) has to
    make the same offline-volume judgement when it counts availability, and two
    copies of this regex would be two chances to disagree about a real drive.
    """
    if not path:
        return None
    vol = _VOLUME_PATH.match(path)
    if vol is None or vol.group(1) in mounted:
        return None
    return vol.group(1)


# ----- rows --------------------------------------------------------------


@dataclass(slots=True)
class TrackRow:
    """One ``tracks`` row, plus the vendor-sourced expected file size."""

    stable_id: str
    title: str | None
    artist: str | None
    isrc: str | None
    duration_ms: int | None
    file_path: str | None
    expected_size_bytes: int | None = None

    @property
    def duration_s(self) -> float | None:
        return None if not self.duration_ms else self.duration_ms / 1000.0

    @property
    def basename(self) -> str:
        return os.path.basename(self.file_path) if self.file_path else ""

    @property
    def title_key(self) -> str:
        return normalise_text(self.title)

    @property
    def artist_key(self) -> str:
        return normalise_text(self.artist)

    @property
    def stem_key(self) -> str:
        return normalise_text(os.path.splitext(self.basename)[0])


def _artist_from_json(raw: str | None) -> str | None:
    """``artists_json`` is a JSON list; join to one comparison string."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip() or None
    if isinstance(parsed, list):
        joined = ", ".join(str(a).strip() for a in parsed if str(a).strip())
        return joined or None
    text = str(parsed).strip()
    return text or None


def load_track_rows(state_db: Path, *, rb_db: Path | None = None) -> list[TrackRow]:
    """Read every ``tracks`` row read-only, enriched with rekordbox FileSize.

    ``rb_db`` is the decrypted rekordbox working copy
    (``data/master.plain.db``); it is the only place a per-track expected file
    size exists, since ``tracks`` has no size column. When it is absent the
    ``basename-size`` tier simply cannot fire -- that is reported loudly by
    the CLI rather than silently defaulted.
    """
    conn = state_db_mod.open_ro(state_db)
    try:
        cur = conn.execute(
            "SELECT stable_id, title, artists_json, isrc, duration_ms, file_path "
            "FROM tracks WHERE deleted_at IS NULL"
        )
        rows = [
            TrackRow(
                stable_id=r[0],
                title=r[1],
                artist=_artist_from_json(r[2]),
                isrc=normalise_isrc(r[3]),
                duration_ms=r[4],
                file_path=r[5],
            )
            for r in cur.fetchall()
        ]
        vendor_ids = dict(
            conn.execute(
                "SELECT stable_id, vendor_id FROM track_vendor_ids "
                "WHERE vendor = 'rekordbox'"
            ).fetchall()
        )
    finally:
        conn.close()

    if rb_db is not None and rb_db.exists() and vendor_ids:
        sizes = _rekordbox_file_sizes(rb_db)
        for row in rows:
            vid = vendor_ids.get(row.stable_id)
            if vid is not None:
                row.expected_size_bytes = sizes.get(str(vid))
    return rows


def _rekordbox_file_sizes(rb_db: Path) -> dict[str, int]:
    """``{djmdContent.ID: FileSize}`` from the decrypted rekordbox copy."""
    conn = sqlite3.connect(f"file:{rb_db}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT ID, FileSize FROM djmdContent WHERE FileSize IS NOT NULL"
        )
        return {str(cid): int(size) for cid, size in cur.fetchall() if size}
    finally:
        conn.close()


# ----- candidates --------------------------------------------------------


@dataclass(slots=True)
class Candidate:
    """One scored replacement path for one row."""

    path: str
    tier: Tier
    confidence: float
    reason: str

    @property
    def auto_applicable(self) -> bool:
        return self.confidence >= AUTO_APPLY_THRESHOLD


def _ratio(a: str, b: str, floor: float) -> float:
    """SequenceMatcher ratio, short-circuited by the cheap upper bounds."""
    if not a or not b:
        return 0.0
    matcher = difflib.SequenceMatcher(None, a, b)
    if matcher.real_quick_ratio() < floor or matcher.quick_ratio() < floor:
        return 0.0
    return matcher.ratio()


def _basename_matches(row: TrackRow, index: DiskIndex) -> list[DiskAudio]:
    if not row.basename:
        return []
    return index.by_basename.get(basename_key(row.basename), [])


def _tier_basename_exact_unique(row: TrackRow, index: DiskIndex) -> list[Candidate]:
    hits = _basename_matches(row, index)
    if len(hits) != 1:
        return []
    return [
        Candidate(
            path=hits[0].path,
            tier="basename-exact-unique",
            confidence=CONF_BASENAME_UNIQUE,
            reason=f"only file named '{row.basename}' under the indexed roots",
        )
    ]


def _tier_basename_duration(row: TrackRow, index: DiskIndex) -> list[Candidate]:
    hits = _basename_matches(row, index)
    if len(hits) < 2 or row.duration_s is None:
        return []
    out: list[Candidate] = []
    for hit in hits:
        if hit.duration_s is None:
            continue
        delta = abs(hit.duration_s - row.duration_s)
        if delta <= DURATION_TOLERANCE_S:
            out.append(
                Candidate(
                    path=hit.path,
                    tier="basename-duration",
                    confidence=CONF_BASENAME_DURATION,
                    reason=(
                        f"basename '{row.basename}' shared by {len(hits)} files; "
                        f"duration {hit.duration_s:.1f}s is within "
                        f"{delta:.1f}s of the recorded {row.duration_s:.1f}s"
                    ),
                )
            )
    return out


def _tier_basename_size(row: TrackRow, index: DiskIndex) -> list[Candidate]:
    hits = _basename_matches(row, index)
    if len(hits) < 2 or not row.expected_size_bytes:
        return []
    return [
        Candidate(
            path=hit.path,
            tier="basename-size",
            confidence=CONF_BASENAME_SIZE,
            reason=(
                f"basename '{row.basename}' shared by {len(hits)} files; "
                f"size {hit.size_bytes} bytes matches the recorded size exactly"
            ),
        )
        for hit in hits
        if hit.size_bytes == row.expected_size_bytes
    ]


def _tier_isrc(row: TrackRow, index: DiskIndex) -> list[Candidate]:
    if not row.isrc:
        return []
    return [
        Candidate(
            path=hit.path,
            tier="isrc",
            confidence=CONF_ISRC,
            reason=f"ISRC tag {row.isrc} matches the recorded ISRC",
        )
        for hit in index.by_isrc.get(row.isrc, [])
    ]


def _fuzzy_pool(row: TrackRow, index: DiskIndex) -> list[DiskAudio]:
    """Blocked candidate pool for the fuzzy tiers, via the token index."""
    text = f"{row.title_key} {row.artist_key} {row.stem_key}"
    tokens = {t for t in text.split() if len(t) >= 4}
    pool: dict[str, DiskAudio] = {}
    for token in tokens:
        postings = index.by_token.get(token)
        if not postings or len(postings) > MAX_TOKEN_POSTINGS:
            continue
        for entry in postings:
            pool[entry.path] = entry
            if len(pool) >= MAX_FUZZY_CANDIDATES:
                return list(pool.values())
    return list(pool.values())


def _tier_title_artist_fuzzy(row: TrackRow, index: DiskIndex) -> list[Candidate]:
    if not row.title_key or not row.artist_key:
        return []
    out: list[Candidate] = []
    for entry in _fuzzy_pool(row, index):
        if not entry.title_key or not entry.artist_key:
            continue
        t_sim = _ratio(row.title_key, entry.title_key, TITLE_SIMILARITY_MIN)
        if t_sim < TITLE_SIMILARITY_MIN:
            continue
        a_sim = _ratio(row.artist_key, entry.artist_key, ARTIST_SIMILARITY_MIN)
        if a_sim < ARTIST_SIMILARITY_MIN:
            continue
        combined = 0.6 * t_sim + 0.4 * a_sim
        out.append(
            Candidate(
                path=entry.path,
                tier="title-artist-fuzzy",
                confidence=round(
                    CONF_TITLE_ARTIST_BASE + CONF_TITLE_ARTIST_SPAN * combined, 3
                ),
                reason=(
                    f"tagged title similarity {t_sim:.2f} and artist similarity "
                    f"{a_sim:.2f} against '{entry.title}' by '{entry.artist}'"
                ),
            )
        )
    return out


def _tier_duration_title_fuzzy(row: TrackRow, index: DiskIndex) -> list[Candidate]:
    if row.duration_s is None or not row.title_key:
        return []
    centre = int(row.duration_s)
    pool: dict[str, DiskAudio] = {}
    for bucket in range(centre - int(DURATION_TOLERANCE_S), centre + int(DURATION_TOLERANCE_S) + 1):
        for entry in index.by_duration_s.get(bucket, []):
            pool[entry.path] = entry
    out: list[Candidate] = []
    for entry in pool.values():
        if entry.duration_s is None:
            continue
        delta = abs(entry.duration_s - row.duration_s)
        if delta > DURATION_TOLERANCE_S:
            continue
        # Tag title when present, else the filename stem -- the reason string
        # says which, so a reviewer knows how strong the text evidence is.
        via, text = ("tagged title", entry.title_key)
        if not text:
            via, text = ("filename", entry.stem_key)
        sim = _ratio(row.title_key, text, TITLE_ONLY_SIMILARITY_MIN)
        if sim < TITLE_ONLY_SIMILARITY_MIN:
            continue
        out.append(
            Candidate(
                path=entry.path,
                tier="duration-title-fuzzy",
                confidence=round(
                    CONF_DURATION_TITLE_BASE + CONF_DURATION_TITLE_SPAN * sim, 3
                ),
                reason=(
                    f"duration within {delta:.1f}s of the recorded "
                    f"{row.duration_s:.1f}s and {via} similarity {sim:.2f}"
                ),
            )
        )
    return out


_TIER_FNS: dict[Tier, Callable[[TrackRow, DiskIndex], list[Candidate]]] = {
    "basename-exact-unique": _tier_basename_exact_unique,
    "basename-duration": _tier_basename_duration,
    "basename-size": _tier_basename_size,
    "isrc": _tier_isrc,
    "title-artist-fuzzy": _tier_title_artist_fuzzy,
    "duration-title-fuzzy": _tier_duration_title_fuzzy,
}


def find_candidates(row: TrackRow, index: DiskIndex) -> list[Candidate]:
    """Candidates from the FIRST tier that fires, best confidence first.

    Tiers are consulted in :data:`TIER_ORDER` and evaluation stops at the
    first tier producing a hit, so ``candidate.tier`` is always the strongest
    signal that was available for this row.
    """
    for tier in TIER_ORDER:
        hits = _TIER_FNS[tier](row, index)
        if hits:
            # Deduplicate by path (a tier can seed the same file twice via
            # different postings), keeping the best confidence.
            best: dict[str, Candidate] = {}
            for cand in hits:
                prior = best.get(cand.path)
                if prior is None or cand.confidence > prior.confidence:
                    best[cand.path] = cand
            return sorted(
                best.values(), key=lambda c: (-c.confidence, len(c.path), c.path)
            )
    return []


# ----- classification ----------------------------------------------------


@dataclass(slots=True)
class RowResult:
    """Classification outcome for exactly one ``tracks`` row."""

    stable_id: str
    file_path: str | None
    bucket: Bucket
    reason: str
    tier: Tier | None = None
    ambiguity: Ambiguity | None = None
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


def _pre_match_bucket(
    row: TrackRow, mounted: frozenset[str]
) -> tuple[Bucket, str] | None:
    """Bucket a row without consulting the disk index, or ``None`` to match.

    Ordered exactly as documented in the module docstring.
    """
    path = row.file_path
    if path and os.path.exists(path):
        return ("present", "recorded path resolves on disk")
    if path is None or not path.strip():
        return ("malformed-path", "file_path is NULL or blank")
    if not path.startswith("/"):
        if _STREAMING_SCHEME.match(path):
            scheme = path.split(":", 1)[0].lower()
            return ("streaming", f"streaming URI ({scheme}:), no local file expected")
        return ("malformed-path", "file_path is not absolute")
    if _SYNTHETIC_CONTENTS.match(path):
        return (
            "malformed-path",
            "synthetic /contents_<digits>/ prefix (USB export artefact)",
        )
    offline = unmounted_volume_of(path, mounted)
    if offline is not None:
        return (
            "awaiting-volume",
            f"volume '{offline}' is not mounted; presumed intact offline",
        )
    return None


def classify_row(
    row: TrackRow,
    index: DiskIndex,
    mounted: frozenset[str],
    *,
    linked_paths: frozenset[str] = frozenset(),
) -> RowResult:
    """Put ``row`` in exactly one bucket, with candidates where relevant.

    ``linked_paths`` are files that already resolve for some OTHER track row.
    A candidate pointing at one of those is kept in the report but barred from
    auto-apply: repointing a second row at an already-linked file would quietly
    create two rows sharing one file, which is corruption dressed as a repair.
    """
    pre = _pre_match_bucket(row, mounted)
    if pre is not None and pre[0] != "malformed-path":
        return RowResult(
            stable_id=row.stable_id,
            file_path=row.file_path,
            bucket=pre[0],
            reason=pre[1],
        )

    candidates = find_candidates(row, index) if row.basename else []

    if pre is not None:
        # Malformed rows still get candidates reported (their basename can be
        # perfectly usable) but the bucket keeps them out of the auto plan.
        return RowResult(
            stable_id=row.stable_id,
            file_path=row.file_path,
            bucket="malformed-path",
            reason=pre[1],
            tier=candidates[0].tier if candidates else None,
            candidates=candidates,
        )

    if not candidates:
        return RowResult(
            stable_id=row.stable_id,
            file_path=row.file_path,
            bucket="absent-no-audio",
            reason="no candidate under the indexed roots",
        )

    above = [c for c in candidates if c.auto_applicable]
    if len(above) == 1:
        if above[0].path in linked_paths:
            return RowResult(
                stable_id=row.stable_id,
                file_path=row.file_path,
                bucket="relinkable-ambiguous",
                reason=(
                    "sole strong candidate is already the resolving path of "
                    "another track row"
                ),
                tier=above[0].tier,
                ambiguity="target-already-linked",
                candidates=candidates,
            )
        return RowResult(
            stable_id=row.stable_id,
            file_path=row.file_path,
            bucket="relinkable-auto",
            reason=above[0].reason,
            tier=above[0].tier,
            candidates=candidates,
        )
    ambiguity: Ambiguity = (
        "multiple-above-threshold" if len(above) > 1 else "below-auto-threshold"
    )
    return RowResult(
        stable_id=row.stable_id,
        file_path=row.file_path,
        bucket="relinkable-ambiguous",
        reason=(
            f"{len(above)} candidates at/above {AUTO_APPLY_THRESHOLD}"
            if above
            else f"best candidate scores {candidates[0].confidence} "
            f"(below {AUTO_APPLY_THRESHOLD})"
        ),
        tier=candidates[0].tier,
        ambiguity=ambiguity,
        candidates=candidates,
    )


def demote_contested_targets(results: list[RowResult]) -> int:
    """Demote auto rows that fight over one file. Returns how many moved.

    Per-row scoring cannot see reverse ambiguity: two different track rows can
    each have exactly one strong candidate and it can be the SAME file.
    Applying those would fan several rows onto one path, so all contenders go
    to review.
    """
    claimants: dict[str, list[RowResult]] = {}
    for res in results:
        if res.bucket == "relinkable-auto" and res.best is not None:
            claimants.setdefault(res.best.path, []).append(res)
    moved = 0
    for path, rows in claimants.items():
        if len(rows) < 2:
            continue
        for res in rows:
            res.bucket = "relinkable-ambiguous"
            res.ambiguity = "target-contested"
            res.reason = (
                f"{len(rows)} track rows each score '{path}' as their sole "
                f"strong candidate"
            )
            moved += 1
    return moved


def classify_rows(
    rows: Sequence[TrackRow], index: DiskIndex, *, mounted: frozenset[str] | None = None
) -> list[RowResult]:
    """Classify every row. Asserts the bucket sum invariant before returning.

    Three passes, because auto-apply safety is not a per-row property:
    1. which recorded paths currently resolve (those files are taken),
    2. per-row tiering/bucketing, barring candidates on taken files,
    3. demote auto rows that contest the same target.
    """
    volumes = mounted_volume_names() if mounted is None else mounted
    linked_paths = frozenset(
        row.file_path
        for row in rows
        if row.file_path and os.path.exists(row.file_path)
    )
    results = [
        classify_row(row, index, volumes, linked_paths=linked_paths) for row in rows
    ]
    demote_contested_targets(results)
    counts = bucket_counts(results)
    total = sum(counts.values())
    if total != len(rows):
        raise AssertionError(
            f"bucket counts sum to {total} but {len(rows)} rows were classified"
        )
    return results


def bucket_counts(results: Sequence[RowResult]) -> dict[str, int]:
    """Counts for every bucket in :data:`ALL_BUCKETS` (zeros included)."""
    counts = {b: 0 for b in ALL_BUCKETS}
    for res in results:
        if res.bucket not in counts:
            raise AssertionError(f"unknown bucket {res.bucket!r}")
        counts[res.bucket] += 1
    return counts


def tier_counts(results: Sequence[RowResult]) -> dict[str, int]:
    counts: dict[str, int] = {t: 0 for t in TIER_ORDER}
    for res in results:
        if res.tier is not None:
            counts[res.tier] += 1
    return counts


# ----- report artefacts --------------------------------------------------

CLASSIFICATION_COLUMNS: tuple[str, ...] = (
    "stable_id",
    "bucket",
    "ambiguity",
    "tier",
    "confidence",
    "candidate_count",
    "recorded_path",
    "best_candidate_path",
    "reason",
)

REVIEW_COLUMNS: tuple[str, ...] = (
    "stable_id",
    "ambiguity",
    "tier",
    "confidence",
    "rank",
    "recorded_path",
    "candidate_path",
    "reason",
)


def write_classification_csv(results: Sequence[RowResult], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CLASSIFICATION_COLUMNS)
        for res in results:
            best = res.best
            writer.writerow(
                [
                    res.stable_id,
                    res.bucket,
                    res.ambiguity or "",
                    res.tier or "",
                    "" if best is None else f"{best.confidence:.3f}",
                    len(res.candidates),
                    res.file_path or "",
                    "" if best is None else best.path,
                    res.reason,
                ]
            )


def write_review_csv(results: Sequence[RowResult], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(REVIEW_COLUMNS)
        for res in results:
            if res.bucket != "relinkable-ambiguous":
                continue
            for rank, cand in enumerate(res.candidates, start=1):
                writer.writerow(
                    [
                        res.stable_id,
                        res.ambiguity or "",
                        cand.tier,
                        f"{cand.confidence:.3f}",
                        rank,
                        res.file_path or "",
                        cand.path,
                        cand.reason,
                    ]
                )


def build_plan(results: Sequence[RowResult]) -> dict[str, object]:
    """Reversible auto-apply plan: every entry carries old AND new path."""
    entries = []
    for res in results:
        if res.bucket != "relinkable-auto":
            continue
        best = res.best
        if best is None:
            raise AssertionError(
                f"relinkable-auto row {res.stable_id} has no candidate"
            )
        entries.append(
            {
                "stable_id": res.stable_id,
                "old_path": res.file_path,
                "new_path": best.path,
                "tier": best.tier,
                "confidence": best.confidence,
                "reason": best.reason,
            }
        )
    return {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "auto_apply_threshold": AUTO_APPLY_THRESHOLD,
        "field": "tracks.file_path",
        "reversible": True,
        "entries": entries,
    }


def write_plan_json(results: Sequence[RowResult], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build_plan(results), indent=1), encoding="utf-8")


# ----- CLI ---------------------------------------------------------------


def _print_report(
    results: Sequence[RowResult], index: DiskIndex, elapsed_s: float
) -> None:
    counts = bucket_counts(results)
    table = Table(
        title=f"Link-repair classification ({len(results)} track rows)",
        show_lines=False,
    )
    table.add_column("Bucket", style="bold")
    table.add_column("Rows", justify="right")
    table.add_column("Share", justify="right")
    total = len(results) or 1
    for bucket in ALL_BUCKETS:
        table.add_row(bucket, str(counts[bucket]), f"{counts[bucket] / total:.1%}")
    table.add_row("[bold]TOTAL[/bold]", f"[bold]{sum(counts.values())}[/bold]", "100.0%")
    console.print(table)

    tiers = tier_counts(results)
    ttable = Table(title="Tier that fired (first-match-wins order)")
    ttable.add_column("Tier", style="bold")
    ttable.add_column("Rows", justify="right")
    for tier in TIER_ORDER:
        ttable.add_row(tier, str(tiers[tier]))
    console.print(ttable)

    amb = [r for r in results if r.bucket == "relinkable-ambiguous"]
    atable = Table(title="Ambiguity reasons")
    atable.add_column("Reason", style="bold")
    atable.add_column("Rows", justify="right")
    for kind in ALL_AMBIGUITIES:
        atable.add_row(kind, str(sum(1 for r in amb if r.ambiguity == kind)))
    console.print(atable)

    used = {r.file_path for r in results if r.bucket == "present" and r.file_path}
    orphans = sum(1 for e in index.entries if e.path not in used)
    console.print(
        f"[dim]Indexed disk files not referenced by any resolving row: "
        f"{orphans} of {len(index.entries)} (the inverse problem).[/dim]"
    )
    console.print(f"[dim]Classified in {elapsed_s:.1f}s.[/dim]")


def _explain(results: Sequence[RowResult], stable_id: str) -> int:
    for res in results:
        if res.stable_id == stable_id:
            console.print(json.dumps(_result_as_dict(res), indent=1))
            return 0
    console.print(f"[red]no row with stable_id {stable_id}[/red]")
    return 1


def _result_as_dict(res: RowResult) -> dict[str, object]:
    return {
        "stable_id": res.stable_id,
        "file_path": res.file_path,
        "bucket": res.bucket,
        "ambiguity": res.ambiguity,
        "tier": res.tier,
        "reason": res.reason,
        "candidates": [asdict(c) for c in res.candidates],
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.reconcile.match",
        description=(
            "Score relink candidates and classify every state.db track row. "
            "Read-only: writes report artefacts, never state.db, never audio."
        ),
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=paths.DATA_DIR,
        help="data dir holding state/state.db and master.plain.db",
    )
    p.add_argument(
        "--roots",
        nargs="+",
        type=Path,
        default=None,
        help=f"disk roots to index (default: {' '.join(str(r) for r in DEFAULT_ROOTS)})",
    )
    p.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="index cache json (default <data-dir>/state/disk-audio-index.json)",
    )
    p.add_argument("--rebuild-index", action="store_true", help="re-read every tag")
    p.add_argument(
        "--limit", type=int, default=None, help="classify only the first N rows"
    )
    p.add_argument(
        "--out-dir", type=Path, default=None, help="report dir (default <data-dir>/reconcile)"
    )
    p.add_argument("--explain", default=None, help="dump one stable_id's candidates")
    p.add_argument(
        "--no-reports", action="store_true", help="print only, write no report files"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    state_db = args.data_dir / "state" / "state.db"
    if not state_db.exists():
        console.print(f"[red]state DB not found at {state_db}[/red]")
        return 1
    rb_db = args.data_dir / "master.plain.db"
    if not rb_db.exists():
        console.print(
            f"[yellow]{rb_db} absent: no expected file sizes, so the "
            f"'basename-size' tier cannot fire this run.[/yellow]"
        )

    console.print(f"[bold]Step 1:[/bold] reading rows from {state_db} (read-only)")
    rows = load_track_rows(state_db, rb_db=rb_db)
    console.print(f"  {len(rows)} track rows")
    if args.limit is not None:
        rows = rows[: args.limit]
        console.print(f"  limited to {len(rows)} rows")

    roots = args.roots if args.roots else list(DEFAULT_ROOTS)
    cache = (
        args.cache
        if args.cache
        else args.data_dir / "state" / CACHE_PATH.name
    )
    console.print(f"[bold]Step 2:[/bold] indexing {', '.join(str(r) for r in roots)}")
    index, stats = build_index(roots, cache_path=cache, rebuild=args.rebuild_index)
    console.print(
        f"  {stats.walked} audio files ({index.unique_basenames()} distinct basenames), "
        f"{stats.tag_reads} tag reads, {stats.reused} cache hits"
    )

    console.print("[bold]Step 3:[/bold] classifying")
    started = time.monotonic()
    results = classify_rows(rows, index)
    elapsed = time.monotonic() - started

    if args.explain:
        return _explain(results, args.explain)

    if not args.no_reports:
        out_dir = args.out_dir if args.out_dir else args.data_dir / "reconcile"
        write_classification_csv(results, out_dir / "link-repair-classification.csv")
        write_review_csv(results, out_dir / "link-repair-review.csv")
        write_plan_json(results, out_dir / "link-repair-plan.json")
        console.print(f"[bold]Step 4:[/bold] reports written to {out_dir}")

    _print_report(results, index, elapsed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
