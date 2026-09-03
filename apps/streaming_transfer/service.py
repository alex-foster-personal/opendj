"""META-05 transfer planning, escalation, reporting, and target write.

Requirements:
✔︎ ✅ 🎯 A Spotify playlist is searched against the official SoundCloud API.
✔︎ ✅ 🎯 Every source resolves to matched, unmatched, or ungradable.
✔︎ ✅ 🎯 Below-threshold candidates take one batched LLM web-search path with a
  mandatory length check before any optional target playlist write.

- if one source disappears from the report then the transfer is broken
- if a low-confidence row reaches a live write before LLM resolution then it is broken
- if a selected LLM target lacks a verified length then acceptance is broken
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from apps.spotify.client import SpotifyPlaylist, SpotifyTrack
from apps.spotify.matcher_adapter import LocalTrack, score_spotify_candidate

from .llm import LlmDecision, OpenRouterBatchMatcher
from .soundcloud import SoundCloudClient, SoundCloudTrack


class TransferContractError(RuntimeError):
    """Transfer input or resolution violates META-05."""


@dataclass(frozen=True)
class TransferRecord:
    source_uri: str
    source_title: str
    target_uri: str | None
    target_title: str | None
    status: str
    confidence: float | None
    match_pass: str
    signals: tuple[str, ...]
    length_verified: bool
    evidence_urls: tuple[str, ...]
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_uri": self.source_uri,
            "source_title": self.source_title,
            "target_uri": self.target_uri,
            "target_title": self.target_title,
            "status": self.status,
            "confidence": self.confidence,
            "match_pass": self.match_pass,
            "signals": list(self.signals),
            "length_verified": self.length_verified,
            "evidence_urls": list(self.evidence_urls),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Escalation:
    source: SpotifyTrack
    candidates: tuple[SoundCloudTrack, ...]
    deterministic_confidence: float
    deterministic_signals: tuple[str, ...]

    @property
    def source_uri(self) -> str:
        return self.source.spotify_uri

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "source": {
                "id": self.source.spotify_uri,
                "title": self.source.title,
                "artists": list(self.source.artists),
                "duration_ms": self.source.duration_ms,
                "isrc": self.source.isrc,
            },
            "targets": [
                {
                    "id": target.urn,
                    "title": target.title,
                    "artists": list(target.artists),
                    "duration_ms": target.duration_ms,
                    "isrc": target.isrc,
                    "permalink_url": target.permalink_url,
                }
                for target in self.candidates
            ],
        }


@dataclass(frozen=True)
class TransferPlan:
    source_service: str
    target_service: str
    source_playlist_id: str
    playlist_name: str
    confidence_threshold: float
    deterministic_matches: tuple[TransferRecord, ...]
    escalations: tuple[Escalation, ...]
    ungradable: tuple[TransferRecord, ...]


@dataclass(frozen=True)
class TransferReport:
    source_service: str
    target_service: str
    source_playlist_id: str
    playlist_name: str
    confidence_threshold: float
    matched: tuple[TransferRecord, ...]
    unmatched: tuple[TransferRecord, ...]
    ungradable: tuple[TransferRecord, ...]
    target_playlist_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_service": self.source_service,
            "target_service": self.target_service,
            "source_playlist_id": self.source_playlist_id,
            "playlist_name": self.playlist_name,
            "confidence_threshold": self.confidence_threshold,
            "target_playlist_id": self.target_playlist_id,
            "counts": {
                "matched": len(self.matched),
                "unmatched": len(self.unmatched),
                "ungradable": len(self.ungradable),
            },
            "buckets": {
                "matched": [row.as_dict() for row in self.matched],
                "unmatched": [row.as_dict() for row in self.unmatched],
                "ungradable": [row.as_dict() for row in self.ungradable],
            },
        }


def _target_as_local(target: SoundCloudTrack) -> LocalTrack:
    return LocalTrack(
        stable_id=target.urn,
        isrc=target.isrc,
        title=target.title,
        artists=target.artists,
        duration_ms=target.duration_ms,
    )


def _ungradable(source: SpotifyTrack, reason: str) -> TransferRecord:
    return TransferRecord(
        source_uri=source.spotify_uri,
        source_title=source.title,
        target_uri=None,
        target_title=None,
        status="ungradable",
        confidence=None,
        match_pass="none",
        signals=(),
        length_verified=False,
        evidence_urls=(),
        reason=reason,
    )


def plan_transfer(
    source: SpotifyPlaylist,
    *,
    target_service: str,
    candidates: Mapping[str, Sequence[SoundCloudTrack]],
    confidence_threshold: float,
) -> TransferPlan:
    """Score every source and explicitly queue every below-threshold candidate."""
    if target_service != "soundcloud":
        raise TransferContractError(f"unsupported transfer target: {target_service!r}")
    if not math.isfinite(confidence_threshold) or not 0 < confidence_threshold <= 1:
        raise TransferContractError("confidence threshold must be finite and within (0, 1]")
    expected = {track.spotify_uri for track in source.tracks}
    missing = expected - set(candidates)
    extras = set(candidates) - expected
    if missing or extras:
        raise TransferContractError(
            f"candidate map mismatch: missing={sorted(missing)!r} extras={sorted(extras)!r}"
        )

    deterministic: list[TransferRecord] = []
    escalations: list[Escalation] = []
    ungradable: list[TransferRecord] = []
    for track in source.tracks:
        if not track.title.strip() or not track.artists or track.duration_ms <= 0:
            ungradable.append(_ungradable(track, "source_metadata_incomplete"))
            continue
        target_rows = tuple(candidates[track.spotify_uri])
        if not target_rows:
            ungradable.append(_ungradable(track, "no_target_candidates"))
            continue
        scored = [
            (*score_spotify_candidate(track, _target_as_local(target)), target)
            for target in target_rows
        ]
        confidence, signals, best = sorted(
            scored,
            key=lambda row: (-row[0], row[2].urn),
        )[0]
        if confidence >= confidence_threshold:
            deterministic.append(
                TransferRecord(
                    source_uri=track.spotify_uri,
                    source_title=track.title,
                    target_uri=best.urn,
                    target_title=best.title,
                    status="matched",
                    confidence=confidence,
                    match_pass="deterministic",
                    signals=signals,
                    length_verified="duration" in signals,
                    evidence_urls=(best.permalink_url,),
                )
            )
        elif confidence < confidence_threshold:
            escalations.append(
                Escalation(
                    source=track,
                    candidates=target_rows,
                    deterministic_confidence=confidence,
                    deterministic_signals=signals,
                )
            )
    return TransferPlan(
        source_service="spotify",
        target_service=target_service,
        source_playlist_id=source.id,
        playlist_name=source.name,
        confidence_threshold=confidence_threshold,
        deterministic_matches=tuple(deterministic),
        escalations=tuple(escalations),
        ungradable=tuple(ungradable),
    )


def resolve_transfer(
    plan: TransferPlan,
    *,
    decisions: Sequence[LlmDecision],
) -> TransferReport:
    """Validate one complete LLM batch and produce the three terminal buckets."""
    expected = {row.source_uri for row in plan.escalations}
    by_source: dict[str, LlmDecision] = {}
    for decision in decisions:
        if decision.source_id in by_source:
            raise TransferContractError(f"duplicate LLM decision: {decision.source_id}")
        by_source[decision.source_id] = decision
    if set(by_source) != expected:
        raise TransferContractError("LLM decision ids do not exactly match the escalation batch")

    matched = list(plan.deterministic_matches)
    unmatched: list[TransferRecord] = []
    for escalation in plan.escalations:
        decision = by_source[escalation.source_uri]
        targets = {candidate.urn: candidate for candidate in escalation.candidates}
        selected = targets.get(decision.target_id) if decision.target_id else None
        if decision.target_id is not None and selected is None:
            raise TransferContractError(f"LLM selected an unoffered target: {decision.target_id!r}")
        accepted = (
            selected is not None
            and decision.length_verified
            and decision.confidence >= plan.confidence_threshold
        )
        if accepted:
            assert selected is not None
            matched.append(
                TransferRecord(
                    source_uri=escalation.source_uri,
                    source_title=escalation.source.title,
                    target_uri=selected.urn,
                    target_title=selected.title,
                    status="matched",
                    confidence=decision.confidence,
                    match_pass="llm_web_search",
                    signals=escalation.deterministic_signals,
                    length_verified=True,
                    evidence_urls=decision.evidence_urls,
                )
            )
        elif not accepted:
            unmatched.append(
                TransferRecord(
                    source_uri=escalation.source_uri,
                    source_title=escalation.source.title,
                    target_uri=selected.urn if selected else None,
                    target_title=selected.title if selected else None,
                    status="unmatched",
                    confidence=decision.confidence,
                    match_pass="llm_web_search",
                    signals=escalation.deterministic_signals,
                    length_verified=decision.length_verified,
                    evidence_urls=decision.evidence_urls,
                    reason="llm_rejected_or_below_threshold",
                )
            )
    return TransferReport(
        source_service=plan.source_service,
        target_service=plan.target_service,
        source_playlist_id=plan.source_playlist_id,
        playlist_name=plan.playlist_name,
        confidence_threshold=plan.confidence_threshold,
        matched=tuple(matched),
        unmatched=tuple(unmatched),
        ungradable=plan.ungradable,
    )


def execute_transfer(
    source: SpotifyPlaylist,
    *,
    target: SoundCloudClient,
    llm: OpenRouterBatchMatcher,
    confidence_threshold: float,
    live: bool,
    sharing: str,
) -> TransferReport:
    """Run real searches, one LLM batch, then at most one target mutation."""
    candidates = {track.spotify_uri: target.search_tracks(track) for track in source.tracks}
    plan = plan_transfer(
        source,
        target_service="soundcloud",
        candidates=candidates,
        confidence_threshold=confidence_threshold,
    )
    decisions = llm.resolve(plan.escalations) if plan.escalations else ()
    report = resolve_transfer(plan, decisions=decisions)
    if not live:
        return report
    target_by_urn = {candidate.urn: candidate for rows in candidates.values() for candidate in rows}
    selected = tuple(
        target_by_urn[row.target_uri] for row in report.matched if row.target_uri is not None
    )
    playlist_id = target.create_playlist(source.name, selected, sharing=sharing)
    return replace(report, target_playlist_id=playlist_id)
