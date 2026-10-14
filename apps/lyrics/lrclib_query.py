"""What the line fetch asks LRCLIB, and when an ASR fallback asks again.

A library title like "7 - Paper Lanterns - Radio Edit" carries a Mixed In Key
energy prefix that LRCLIB never indexes, so the raw title always missed and
the track fell through to the Whisper transcript, which mishears a sung
chorus often enough to show wrong words. The query strips that prefix
with the same floor the offline sources pipeline uses; version suffixes stay,
because they change which lyrics are correct.

A track already cached from the ASR fallback asks LRCLIB again on its next
fetch, so it is replaced once LRCLIB has lines. A failed lookup, or a sheet
that does not parse, keeps the ASR lines served rather than failing a read
that used to work. The library-jobs lane re-runs an ASR-cached track once its
last LRCLIB ask is older than ASR_LRCLIB_RECHECK, so a stale ASR cache is not
"up to date" forever and LRCLIB is not asked on every sweep.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from apps.lyrics.asr_hallucination import ASR_SOURCE
from apps.lyrics.fetch_verdicts import FetchVerdict, load_verdict, utc_now_iso, write_verdict
from apps.lyrics.sources.library import normalize_title

if TYPE_CHECKING:
    from apps.lyrics.cache import LyricLine, Lyrics
    from apps.lyrics.service import Track


class _SyncedProvider(Protocol):
    def fetch_synced(self, track: Track) -> str | None: ...


def lrclib_title(title: str) -> str:
    """The title LRCLIB is asked for: without a MIK key/energy prefix."""
    return normalize_title(title) or title


def lrclib_query_params(track: Track) -> dict[str, str | int]:
    """The LRCLIB exact-get query for a library track."""
    return {"artist_name": track.artist, "track_name": lrclib_title(track.title), "duration": track.duration_s}


def asr_cache_upgrade(
    provider: _SyncedProvider,
    cached: Lyrics,
    track: Track,
    parse: Callable[[str], tuple[LyricLine, ...]],
) -> tuple[LyricLine, ...] | None:
    """Parsed LRCLIB lines for a track whose cache holds only the ASR fallback, else None.

    A lookup error and an unparseable sheet both return None, so the caller
    keeps serving the ASR lines.
    """
    if cached.source != ASR_SOURCE:
        return None
    try:
        synced = provider.fetch_synced(track)
        return parse(synced) if synced is not None else None
    except (RuntimeError, OSError, ValueError):
        return None


ASR_LRCLIB_RECHECK = timedelta(days=7)


def asr_recheck_due(verdict: FetchVerdict | None, now: datetime) -> bool:
    """Whether an ASR-cached track is due another LRCLIB ask.

    Due when no ASR verdict records the last ask, or that ask is at least
    ASR_LRCLIB_RECHECK old.
    """
    if verdict is None or verdict.outcome != "cached" or verdict.source != ASR_SOURCE:
        return True
    return now - datetime.fromisoformat(verdict.recorded_at) >= ASR_LRCLIB_RECHECK


def mark_asr_rechecked(data_dir: Path, stable_id: str, vocals_sha256: str | None) -> None:
    """Record that LRCLIB was asked again for an ASR-cached track and the ASR lines stay."""
    previous = load_verdict(data_dir, stable_id)
    if previous is not None and previous.outcome == "cached" and previous.source == ASR_SOURCE:
        write_verdict(data_dir, replace(previous, recorded_at=utc_now_iso()))
        return
    write_verdict(
        data_dir,
        FetchVerdict(
            stable_id=stable_id,
            outcome="cached",
            source="asr",
            vocals_sha256=vocals_sha256,
            hub_code=None,
            hub_message=None,
            language_iso3=None,
            recorded_at=utc_now_iso(),
        ),
    )
