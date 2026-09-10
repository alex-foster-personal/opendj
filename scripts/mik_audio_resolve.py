#!/usr/bin/env python3
"""Resolve MIK ``ZSONG`` rows to audio that EXISTS on disk, with provenance.

Why this exists: the equivalence suite can only reach an UNTESTED verdict on
anything that needs the audio itself (MIK's undocumented ``ZSONG.ZVOLUME``, an
independent key opinion). Both need a subset of MIK rows whose file we can
actually decode, and that subset has to carry its provenance, because how we
found the file changes how much the measurement is worth.

Three resolution tiers, reported separately and never as one headline number:

============  ================================================================
tier          meaning
============  ================================================================
``bookmark``  MIK's OWN Apple-bookmark path resolves. Highest confidence: this
              is the exact file MIK analysed, so a matcher error cannot be the
              explanation for a mismatch
``rekordbox`` the suite's MIK-to-rekordbox pairing resolved and
              ``djmdContent.FolderPath`` exists on disk. Inherits that pair's
              tier (``exact_path`` / ``basename`` / ``artist_title``)
``state_db``  neither of the above, but a ``tracks`` row whose ``file_path``
              exists has the same basename as the paired rekordbox row
============  ================================================================

MIK-AUDIT section 3 recorded that NOT ONE MIK stored path resolved. That is now
stale: re-measured here Tue 28 Jul 2026, 575 of 7,026 decoded bookmark paths
resolve, because the ``af--link-repair`` workflow has been relocating files back
under the owner's home directory. Re-run rather than quoting either number.

Stdlib only on purpose: the heavy-dependency measurement scripts import this
module from their own PEP 723 environments.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.equivalence.sources import (
    MikRow,
    match,
    read_mik,
    read_rekordbox,
)

MIK_DB = Path.home() / "Library/Application Support/Mixedinkey/Collection10.mikdb"
CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
AUDIO_SUFFIXES = frozenset(
    {".mp3", ".m4a", ".aiff", ".aif", ".wav", ".flac", ".aac", ".ogg", ".opus", ".alac"}
)


def core_data_time(value: float | None) -> datetime | None:
    """MIK stores Core Data timestamps: seconds since 2001-01-01 UTC."""
    if value is None:
        return None
    return CORE_DATA_EPOCH + timedelta(seconds=float(value))


@dataclass(frozen=True)
class ResolvedAudio:
    """One MIK row we can decode, plus everything needed to judge it later."""

    mik_pk: int
    path: str
    tier: str
    pair_tier: str | None
    rekordbox_id: str | None
    title: str | None
    artist: str | None
    volume: float | None
    energy: float | None
    tempo: float | None
    key_camelot: str | None
    key_confidence: float | None
    clipped_peak_count: int | None
    analysis_date_iso: str | None
    file_mtime_iso: str
    file_size: int
    reencoded_after_analysis: bool
    """``True`` when the file was modified AFTER MIK analysed it. MIK's numbers
    then describe a DIFFERENT byte stream from the one we measure, so such a row
    is a legitimate mismatch that says nothing about the unit."""


def _analysis_dates(db_path: Path) -> dict[int, float | None]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {
            int(pk): date
            for pk, date in conn.execute("SELECT Z_PK, ZANALYSISDATE FROM ZSONG")
        }
    finally:
        conn.close()


def _present_basenames(state_db: Path) -> dict[str, str]:
    """Basename to path, for ``tracks`` rows whose ``file_path`` exists NOW.

    Two present tracks sharing a basename is ambiguous, not a pick: the
    fallback below has no other signal to prefer one over the other, and
    silently keeping whichever row SQLite happened to return first can feed
    an unrelated track's audio into the loudness/key experiment. An
    ambiguous basename resolves to no candidate at all rather than a guess.

    ``deleted_at IS NULL`` is required (P1 regression, PR #383 review): a
    soft-deleted track's row still carries a ``file_path`` that can exist on
    disk (a soft delete never touches the file), so an unfiltered query would
    admit a tombstoned track as the sole basename candidate and let its
    unrelated audio be fed into the loudness/key experiment used to justify
    field mappings.
    """
    if not state_db.exists():
        raise FileNotFoundError(f"state.db not found: {state_db}")
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT file_path FROM tracks "
            "WHERE file_path IS NOT NULL AND deleted_at IS NULL"
        )
        candidates: dict[str, set[str]] = {}
        for (path,) in rows:
            if path and os.path.exists(path):
                candidates.setdefault(Path(path).name.casefold(), set()).add(path)
        return {
            name: next(iter(paths))
            for name, paths in candidates.items()
            if len(paths) == 1
        }
    finally:
        conn.close()


def _is_audio(path: str) -> bool:
    return Path(path).suffix.casefold() in AUDIO_SUFFIXES


def _describe(
    mik: MikRow,
    path: str,
    tier: str,
    pair_tier: str | None,
    rekordbox_id: str | None,
    analysis_epoch: float | None,
) -> ResolvedAudio:
    stat = os.stat(path)
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    analysed = core_data_time(analysis_epoch)
    return ResolvedAudio(
        mik_pk=mik.pk,
        path=path,
        tier=tier,
        pair_tier=pair_tier,
        rekordbox_id=rekordbox_id,
        title=mik.title,
        artist=mik.artist,
        volume=mik.volume,
        energy=mik.energy,
        tempo=mik.tempo,
        key_camelot=mik.key_camelot,
        key_confidence=mik.key_confidence,
        clipped_peak_count=mik.clipped_peak_count,
        analysis_date_iso=None if analysed is None else analysed.isoformat(),
        file_mtime_iso=mtime.isoformat(),
        file_size=stat.st_size,
        reencoded_after_analysis=analysed is not None and mtime > analysed,
    )


def resolve(data_dir: Path, mik_db: Path = MIK_DB) -> list[ResolvedAudio]:
    """Every MIK row we can decode, best tier first, each row at most once."""
    rb_rows = read_rekordbox(data_dir / "master.plain.db")
    mik_rows = read_mik(mik_db)
    analysis = _analysis_dates(mik_db)
    pairings, _report = match(rb_rows, mik_rows)
    paired = {p.right.pk: p for p in pairings}
    present = _present_basenames(data_dir / "state" / "state.db")

    resolved: list[ResolvedAudio] = []
    for mik in mik_rows:
        epoch = analysis.get(mik.pk)
        pair = paired.get(mik.pk)
        if mik.path and _is_audio(mik.path) and os.path.isfile(mik.path):
            resolved.append(
                _describe(
                    mik,
                    mik.path,
                    "bookmark",
                    None if pair is None else pair.tier,
                    None if pair is None else pair.left.content_id,
                    epoch,
                )
            )
            continue
        if pair is None:
            continue
        rb_path = pair.left.path
        if rb_path and _is_audio(rb_path) and os.path.isfile(rb_path):
            resolved.append(
                _describe(
                    mik, rb_path, "rekordbox", pair.tier, pair.left.content_id, epoch
                )
            )
            continue
        names = [pair.left.file_name]
        if rb_path:
            names.append(Path(rb_path).name)
        for name in names:
            if not name:
                continue
            hit = present.get(name.casefold())
            if hit and _is_audio(hit) and os.path.isfile(hit):
                resolved.append(
                    _describe(
                        mik, hit, "state_db", pair.tier, pair.left.content_id, epoch
                    )
                )
                break
    return resolved


def tier_counts(rows: list[ResolvedAudio]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[row.tier] = out.get(row.tier, 0) + 1
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--mik-db", default=MIK_DB, type=Path)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    rows = resolve(args.data_dir, args.mik_db)
    counts = tier_counts(rows)
    suspect = sum(1 for r in rows if r.reencoded_after_analysis)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mik_db": str(args.mik_db),
        "resolved": len(rows),
        "by_tier": counts,
        "reencoded_after_analysis": suspect,
        "rows": [asdict(r) for r in rows],
    }
    out = args.out or args.data_dir / "state" / "equivalence-audio-map.json"
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"resolved {len(rows)} MIK rows to audio on disk, by tier {counts}")
    print(f"file modified after MIK analysed it: {suspect}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
