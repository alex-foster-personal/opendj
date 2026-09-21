"""Candidate-set contract: what the fetch thread hands the version-verifier thread.

One JSON per track under data/state/lyrics-eval/candidates/<track_key>.json:

  {"schema_version": 1, "track": {...}, "generated_at": iso,
   "candidates": [{"id", "source", "method", "rank", "plain_text",
                   "synced_lrc", "source_track", "duration_delta_s",
                   "retrieved_at"}]}

Rules of the contract:
- The fetch side NEVER picks a winner. Every plausible text ships, ranked by
  match strength (exact duration-anchored first, then search hits by API
  order), with enough provenance for the verifier to arbitrate.
- duration_delta_s is the strongest cheap wrong-version signal we can attach;
  null means the source did not report a duration.
- A track file with zero candidates is still written: it is the explicit
  "no source found" record that routes the track to the ASR thread.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from apps.lyrics.sources.base import STATE_DIR, CheckResult, Track, http_get_json
from apps.lyrics.sources.lrclib import BASE as LRCLIB_BASE
from apps.lyrics.sources.lrclib import LrclibProvider

CANDIDATES_DIR = STATE_DIR / "candidates"
SCHEMA_VERSION = 1
SEARCH_TOP_K = 3

# 29% of the present crate is remix-suffixed; the ORIGINAL's lyrics are the
# best candidate text for most of them (the verifier thread arbitrates cuts
# and changed verses). Suffix stripped for CANDIDATE GENERATION only -- the
# track's own title/norm_title stay intact everywhere else.
REMIX_SUFFIX_RE = re.compile(
    r"\s*[-(\[]\s*([^-()\[\]]*?)\s*(remix|mix|edit|rework|remake|bootleg|flip|vip|dub|version)\s*[)\]]?\s*$",
    re.IGNORECASE,
)

# Relaxed-recovery strips: sources index a bare title while a library copy
# carries a featuring clause and a remix suffix. These run ONLY on tracks
# every anchored query already missed, and every hit ships as a last-rank
# candidate with a relaxed-* method tag -- the verifier arbitrates.
FEAT_CLAUSE_RE = re.compile(
    r"\s*[(\[]?\s*(?:feat|ft|featuring)\.?\s+[^)\]]+[)\]]?", re.IGNORECASE
)


def remix_base_title(norm_title: str) -> str | None:
    """The title minus one trailing remix/version suffix; None when there is
    no suffix or stripping would leave nothing."""
    m = REMIX_SUFFIX_RE.search(norm_title)
    if m is None:
        return None
    base = norm_title[: m.start()].strip()
    return base or None


def strip_feat_clause(title: str) -> str | None:
    """Title minus an embedded feat/ft clause; None when nothing changed."""
    stripped = FEAT_CLAUSE_RE.sub("", title).strip(" -").strip()
    return stripped if stripped and stripped != title else None


def strip_trailing_segment(title: str) -> str | None:
    """Title minus its LAST ' - X' segment (broader than REMIX_SUFFIX_RE:
    catches '- ATFC's Vocal', '- Club Cut'...). None when there is no dash
    segment or stripping would leave nothing."""
    head, sep, _ = title.rpartition(" - ")
    if not sep:
        return None
    head = head.strip()
    return head if head and head != title else None

#-----------------------------------------------------------------------------


def _candidate(source: str, method: str, rank: int, plain: str | None, synced: str | None,
               source_track: dict, duration_delta_s: float | None) -> dict:
    return {
        "id": f"{source}-{method}-{rank}",
        "source": source,
        "method": method,
        "rank": rank,
        "plain_text": plain,
        "synced_lrc": synced,
        "source_track": source_track,
        "duration_delta_s": duration_delta_s,
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def lrclib_candidates(provider: LrclibProvider, track: Track) -> tuple[list[dict], list[dict]]:
    """Returns (candidates, instrumental_evidence). Exact duration-anchored hit
    (rank 0) + top-K loose search hits, deduped by LRCLIB record id. Search
    responses carry full lyric bodies, so no extra per-candidate calls are
    needed. A source positively declaring the track INSTRUMENTAL is recorded
    as evidence, distinct from "no source found" -- instrumental tracks must
    not be scraped for (wrong) lyrics nor routed to ASR as failures."""
    candidates: list[dict] = []
    instrumental_evidence: list[dict] = []
    seen_ids: set[int] = set()
    exact = provider._get(track)
    if exact is not None and exact.get("instrumental"):
        instrumental_evidence.append({
            "source": "lrclib", "method": "exact-get",
            "source_id": exact.get("id"),
            "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        })
    if exact is not None and (exact.get("plainLyrics") or exact.get("syncedLyrics")):
        seen_ids.add(exact.get("id", -1))
        candidates.append(_candidate(
            "lrclib", "exact-get", 0,
            exact.get("plainLyrics") or None, exact.get("syncedLyrics") or None,
            {"artist": exact.get("artistName"), "title": exact.get("trackName"),
             "album": exact.get("albumName"), "duration_s": exact.get("duration"),
             "source_id": exact.get("id")},
            (float(exact["duration"]) - track.duration_s) if exact.get("duration") else None,
        ))
    rank = _append_search_candidates(
        candidates, seen_ids, track, track.norm_title, "search", rank_start=1)
    base = remix_base_title(track.norm_title)
    if base is not None:
        _append_search_candidates(
            candidates, seen_ids, track, base, "original-of-remix", rank_start=rank)
    return candidates, instrumental_evidence


def _append_search_candidates(candidates: list[dict], seen_ids: set[int], track: Track,
                              query_title: str, method: str, rank_start: int) -> int:
    _, results = http_get_json(f"{LRCLIB_BASE}/search", {
        "track_name": query_title, "artist_name": track.primary_artist,
    })
    rank = rank_start
    for r in results or []:
        if rank >= rank_start + SEARCH_TOP_K:
            break
        if r.get("id") in seen_ids or not (r.get("plainLyrics") or r.get("syncedLyrics")):
            continue
        seen_ids.add(r.get("id"))
        candidates.append(_candidate(
            "lrclib", method, rank,
            r.get("plainLyrics") or None, r.get("syncedLyrics") or None,
            {"artist": r.get("artistName"), "title": r.get("trackName"),
             "album": r.get("albumName"), "duration_s": r.get("duration"),
             "source_id": r.get("id")},
            (float(r["duration"]) - track.duration_s) if r.get("duration") else None,
        ))
        rank += 1
    return rank


def relaxed_recovery_candidates(_provider: LrclibProvider, track: Track) -> list[dict]:
    """Recovery tier for tracks the anchored queries left EMPTY: retry the
    loose search with progressively relaxed titles (drop feat clause, drop the
    last dash segment, both). Hits keep full LRCLIB provenance + duration
    deltas; the relaxed-* method tag is the weak-query warning label."""
    candidates: list[dict] = []
    seen_ids: set[int] = set()
    variants: list[tuple[str, str]] = []
    no_feat = strip_feat_clause(track.norm_title)
    if no_feat is not None:
        variants.append((no_feat, "relaxed-no-feat"))
    no_suffix = strip_trailing_segment(track.norm_title)
    if no_suffix is not None:
        variants.append((no_suffix, "relaxed-no-suffix"))
    if no_feat is not None:
        bare = strip_trailing_segment(no_feat)
        if bare is not None and bare not in {v for v, _ in variants}:
            variants.append((bare, "relaxed-bare"))
    rank = 0
    for query_title, method in variants:
        rank = _append_search_candidates(
            candidates, seen_ids, track, query_title, method, rank_start=rank)
    return candidates


def write_candidate_set(
    track: Track, candidates: list[dict], instrumental_evidence: list[dict] | None = None
) -> Path:
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CANDIDATES_DIR / f"{track.key}.json"
    out_path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "track": {"key": track.key, "artist": track.artist, "title": track.title,
                  "norm_title": track.norm_title, "duration_s": track.duration_s,
                  "audio_path": track.audio_path},
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "candidates": candidates,
        "instrumental_evidence": instrumental_evidence or [],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return out_path


def check_from_candidates(candidates: list[dict]) -> CheckResult:
    if not candidates:
        return CheckResult(found=False, detail="no-candidates")
    return CheckResult(
        found=True,
        synced=any(c["synced_lrc"] for c in candidates),
        detail=f"n:{len(candidates)}",
    )

#-----------------------------------------------------------------------------

TRACK_REQUIRED_KEYS = ("key", "artist", "title", "norm_title", "duration_s", "audio_path")
CANDIDATE_REQUIRED_KEYS = (
    "id", "source", "method", "rank", "plain_text", "synced_lrc",
    "source_track", "duration_delta_s", "retrieved_at",
)


def validate_candidate_file(d: dict, expected_key: str | None = None) -> None:
    """The verifier thread consumes these files blind; a malformed one fails
    HERE, loudly, not over there. Raises ValueError naming the violation."""
    if d.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version {d.get('schema_version')!r} != {SCHEMA_VERSION}")
    track = d.get("track")
    if not isinstance(track, dict):
        raise ValueError("missing track object")  # noqa: TRY004 -- one exception type is the API
    for k in TRACK_REQUIRED_KEYS:
        if k not in track:
            raise ValueError(f"track missing {k!r}")
    if expected_key is not None and track["key"] != expected_key:
        raise ValueError(f"track.key {track['key']!r} != filename key {expected_key!r}")
    candidates = d.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a list")  # noqa: TRY004 -- one exception type is the API
    for i, c in enumerate(candidates):
        for k in CANDIDATE_REQUIRED_KEYS:
            if k not in c:
                raise ValueError(f"candidates[{i}] missing {k!r}")
        if c["plain_text"] is None and c["synced_lrc"] is None:
            raise ValueError(f"candidates[{i}] carries no text at all")
    if not isinstance(d.get("instrumental_evidence", []), list):
        raise ValueError("instrumental_evidence must be a list")  # noqa: TRY004 -- one exception type is the API
    if d.get("instrumental_evidence") and candidates:
        for ev in d["instrumental_evidence"]:
            if ev.get("method") == "exact-get" and any(c["method"] == "exact-get" for c in candidates):
                raise ValueError("exact-get both declared instrumental AND supplied text")


def bucket_of(d: dict) -> str:
    """The one partition every coverage figure hangs off: instrumental-declared
    beats candidates beats no-source; genius-only split out because those
    tracks rest entirely on unverified scrapes."""
    if d.get("instrumental_evidence"):
        return "instrumental-declared"
    candidates = d.get("candidates", [])
    if not candidates:
        return "no-source"
    if all(c["source"] == "genius" for c in candidates):
        return "genius-only"
    return "has-candidates"
