"""Cheap heuristic gate for BC3 dirty-metadata enrich pipeline candidates.

Runs only local regex rules from ``title_fix``; no LLM calls, no persistence,
and no library UI changes. Parent: dirty-metadata enrich pipeline (issue #334).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from apps.tags.title_fix import (
    SAFE_TRANSFORMS,
    Proposal,
    Rx,
    Track,
    _same,
    mashup_candidate,
    project_rules,
    regex_propose,
    safe_tier,
)

_EVALS = Path(__file__).resolve().parent / "evals" / "dirty_metadata_cases.jsonl"

_JUNK_RAW = (
    Rx.SITE,
    Rx.JUNK,
    Rx.DOWNLOAD_TAIL,
    Rx.SITE_MARK,
    Rx.MIK_PREFIX,
    Rx.MIK_PAREN,
    Rx.MOJIBAKE,
    Rx.CUE_TIME,
    Rx.PROMO_STARS,
    Rx.BITRATE_BRACKET,
    Rx.BEATPORT_ID,
)


@dataclass(frozen=True)
class DirtyMetadataDecision:
    candidate: bool
    skip_reason: str = ""


def dirty_metadata_candidate(
    track: Track,
    *,
    eval_tags: Iterable[str] = (),
    approved: bool = False,
) -> DirtyMetadataDecision:
    """Return whether a row should enter the BC3 dirty-metadata enrich pipeline."""
    tags = frozenset(eval_tags)
    if "must_not_overwrite" in tags:
        return DirtyMetadataDecision(False, "must_not_overwrite")
    if approved:
        return DirtyMetadataDecision(False, "approved")
    proposal = regex_propose(track)
    if _is_store_clean(track, proposal):
        return DirtyMetadataDecision(False, "store_clean")
    if _is_artistic_edge(track, proposal):
        return DirtyMetadataDecision(False, "artistic_edge")
    strong, _weak = project_rules(track)
    if strong:
        return DirtyMetadataDecision(False, "dj_project")
    if _is_obviously_dirty(track, proposal):
        return DirtyMetadataDecision(True)
    return DirtyMetadataDecision(False, "not_dirty_enough")


def _field_has_junk(text: str) -> bool:
    return any(pattern.search(text) for pattern in _JUNK_RAW)


def _is_store_clean(track: Track, proposal: Proposal) -> bool:
    if proposal.review_reasons:
        return False
    if "_" in track.title and re.search(r"_\d+|_pn\b|_capture\b", track.title, re.I):
        return False
    if safe_tier(track, proposal):
        return True
    baseline = Proposal(track.artist.strip(), track.title.strip())
    if _same(proposal, baseline) and not _field_has_junk(track.title) and not _field_has_junk(track.artist):
        return True
    return False


def _stylized_all_caps(title: str) -> bool:
    """ALL CAPS styling that title_fix treats as artistic, not junk."""
    if sum(c.isupper() for c in title) <= 3 or any(c.islower() for c in title):
        return False
    raw_words = len(title.split())
    return not (raw_words >= 3 and bool(Rx.CAPS_JUNK.search(title)))


def _is_artistic_edge(track: Track, proposal: Proposal) -> bool:
    title = track.title.strip()
    artist = track.artist.strip()
    if title and _stylized_all_caps(title):
        return True
    if Rx.MASHUP_JOIN.search(title) and not mashup_candidate(track, proposal):
        return True
    if re.search(r"\bvideo\b", title, re.I) and not _field_has_junk(title):
        return True
    if re.fullmatch(r"[a-z]_[a-z]", artist):
        return True
    if "," in artist and not Rx.SITE_MARK.search(artist):
        return True
    if re.search(r"\bS\.W\.A\.Y\.|MODEST\.|RIOT\b", title):
        return True
    return False


def _artist_jammed_in_title(track: Track, proposal: Proposal) -> bool:
    if track.artist.strip():
        return False
    return bool(proposal.artist.strip()) and proposal.artist != track.artist.strip()


def _unsafe_transforms(track: Track, proposal: Proposal) -> bool:
    if not proposal.transforms:
        return False
    if safe_tier(track, proposal):
        return False
    if set(proposal.transforms) <= set(SAFE_TRANSFORMS):
        return _artist_jammed_in_title(track, proposal) or bool(proposal.review_reasons)
    return True


def _watermark_artist(track: Track) -> bool:
    artist = track.artist.strip()
    if not artist:
        return False
    if Rx.SITE_MARK.search(artist):
        return True
    title_fold = re.sub(r"\W+", "", track.title).casefold()
    artist_fold = re.sub(r"\W+", "", artist).casefold()
    return bool(title_fold) and title_fold == artist_fold and bool(Rx.ARTIST_SPLIT.search(artist))


def _is_obviously_dirty(track: Track, proposal: Proposal) -> bool:
    if _field_has_junk(track.title) or _field_has_junk(track.artist):
        return True
    if _watermark_artist(track):
        return True
    if _unsafe_transforms(track, proposal):
        return True
    if _artist_jammed_in_title(track, proposal):
        return True
    if "_" in track.title and re.search(r"_\d{3}_|_pn\b|_capture\b", track.title, re.I):
        return True
    return False


def default_eval_cases_path() -> Path:
    return _EVALS


def load_dirty_metadata_cases(path: Path | None = None) -> list[dict[str, Any]]:
    src = path or _EVALS
    rows: list[dict[str, Any]] = []
    for line in src.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def eval_case_track(case: Mapping[str, Any]) -> Track:
    return Track(
        str(case.get("id", "")),
        str(case["title"]),
        str(case.get("artist", "")),
        str(case.get("filename", "")),
    )


def eval_case_decision(case: Mapping[str, Any]) -> DirtyMetadataDecision:
    tags = case.get("tags") or ()
    if isinstance(tags, str):
        tags = (tags,)
    return dirty_metadata_candidate(
        eval_case_track(case),
        eval_tags=tags,
        approved=bool(case.get("approved")),
    )
