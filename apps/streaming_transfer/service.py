"""META-05 Spotify-to-SoundCloud transfer contract.

- if one source disappears from the report then the transfer is broken
- if a low-confidence row reaches a live write before LLM resolution then it is broken
- if selected provider durations exceed tolerance then acceptance is broken
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
    source: SpotifyTrack
    target: SoundCloudTrack | None
    status: str
    confidence: float | None
    match_pass: str
    signals: tuple[str, ...]
    duration_tolerance_ms: int
    evidence_urls: tuple[str, ...]
    web_search_batch_id: str | None
    web_search_requests: int | None
    reason: str | None = None

    @property
    def source_uri(self) -> str:
        return self.source.spotify_uri

    @property
    def target_uri(self) -> str | None:
        return self.target.urn if self.target else None

    @property
    def duration_delta_ms(self) -> int | None:
        return abs(self.source.duration_ms - self.target.duration_ms) if self.target else None

    @property
    def duration_within_tolerance(self) -> bool | None:
        delta = self.duration_delta_ms
        return delta <= self.duration_tolerance_ms if delta is not None else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_uri": self.source_uri,
            "source_title": self.source.title,
            "target_uri": self.target_uri,
            "target_title": self.target.title if self.target else None,
            "status": self.status,
            "confidence": self.confidence,
            "match_pass": self.match_pass,
            "signals": list(self.signals),
            "source_duration_ms": self.source.duration_ms,
            "target_duration_ms": self.target.duration_ms if self.target else None,
            "duration_delta_ms": self.duration_delta_ms,
            "duration_tolerance_ms": self.duration_tolerance_ms,
            "duration_within_tolerance": self.duration_within_tolerance,
            "evidence_urls": list(self.evidence_urls),
            "web_search_batch_id": self.web_search_batch_id,
            "web_search_requests": self.web_search_requests,
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
    duration_tolerance_ms: int
    deterministic_matches: tuple[TransferRecord, ...]
    deterministic_unmatched: tuple[TransferRecord, ...]
    escalations: tuple[Escalation, ...]
    ungradable: tuple[TransferRecord, ...]


@dataclass(frozen=True)
class TransferReport:
    source_service: str
    target_service: str
    source_playlist_id: str
    playlist_name: str
    confidence_threshold: float
    duration_tolerance_ms: int
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
            "duration_tolerance_ms": self.duration_tolerance_ms,
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


def _ungradable(
    source: SpotifyTrack,
    reason: str,
    duration_tolerance_ms: int,
) -> TransferRecord:
    return TransferRecord(
        source=source,
        target=None,
        status="ungradable",
        confidence=None,
        match_pass="none",
        signals=(),
        duration_tolerance_ms=duration_tolerance_ms,
        evidence_urls=(),
        web_search_batch_id=None,
        web_search_requests=None,
        reason=reason,
    )


def plan_transfer(
    source: SpotifyPlaylist,
    *,
    target_service: str,
    candidates: Mapping[str, Sequence[SoundCloudTrack]],
    confidence_threshold: float,
    duration_tolerance_ms: int,
) -> TransferPlan:
    """Score every source and explicitly queue every below-threshold candidate."""
    if target_service != "soundcloud":
        raise TransferContractError(f"unsupported transfer target: {target_service!r}")
    if not math.isfinite(confidence_threshold) or not 0 < confidence_threshold <= 1:
        raise TransferContractError("confidence threshold must be finite and within (0, 1]")
    if (
        not isinstance(duration_tolerance_ms, int)
        or isinstance(duration_tolerance_ms, bool)
        or duration_tolerance_ms <= 0
    ):
        raise TransferContractError("duration tolerance must be a positive integer")
    expected = {track.spotify_uri for track in source.tracks}
    missing = expected - set(candidates)
    extras = set(candidates) - expected
    if missing or extras:
        raise TransferContractError(
            f"candidate map mismatch: missing={sorted(missing)!r} extras={sorted(extras)!r}"
        )

    deterministic: list[TransferRecord] = []
    deterministic_unmatched: list[TransferRecord] = []
    escalations: list[Escalation] = []
    ungradable: list[TransferRecord] = []
    for track in source.tracks:
        if not track.title.strip() or not track.artists or track.duration_ms <= 0:
            ungradable.append(
                _ungradable(track, "source_metadata_incomplete", duration_tolerance_ms)
            )
            continue
        target_rows = tuple(candidates[track.spotify_uri])
        if not target_rows:
            ungradable.append(_ungradable(track, "no_target_candidates", duration_tolerance_ms))
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
            record = TransferRecord(
                source=track,
                target=best,
                status="matched",
                confidence=confidence,
                match_pass="deterministic",
                signals=signals,
                duration_tolerance_ms=duration_tolerance_ms,
                evidence_urls=(best.permalink_url,),
                web_search_batch_id=None,
                web_search_requests=None,
            )
            if record.duration_within_tolerance is True:
                deterministic.append(record)
            elif record.duration_within_tolerance is False:
                deterministic_unmatched.append(
                    replace(record, status="unmatched", reason="duration_mismatch")
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
        duration_tolerance_ms=duration_tolerance_ms,
        deterministic_matches=tuple(deterministic),
        deterministic_unmatched=tuple(deterministic_unmatched),
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
    unmatched = list(plan.deterministic_unmatched)
    for escalation in plan.escalations:
        decision = by_source[escalation.source_uri]
        targets = {candidate.urn: candidate for candidate in escalation.candidates}
        selected = targets.get(decision.target_id) if decision.target_id else None
        if decision.target_id is not None and selected is None:
            raise TransferContractError(f"LLM selected an unoffered target: {decision.target_id!r}")
        duration_mismatch = selected is not None and (
            abs(escalation.source.duration_ms - selected.duration_ms)
            > plan.duration_tolerance_ms
        )
        accepted = selected is not None and not duration_mismatch and (
            decision.confidence >= plan.confidence_threshold
        )
        reason = None if accepted else "llm_rejected_or_below_threshold"
        if duration_mismatch:
            reason = "duration_mismatch"
        record = TransferRecord(
            source=escalation.source,
            target=selected,
            status="matched" if accepted else "unmatched",
            confidence=decision.confidence,
            match_pass="llm_web_search",
            signals=escalation.deterministic_signals,
            duration_tolerance_ms=plan.duration_tolerance_ms,
            evidence_urls=decision.evidence_urls,
            web_search_batch_id=decision.web_search_batch_id,
            web_search_requests=decision.web_search_requests,
            reason=reason,
        )
        if accepted:
            matched.append(record)
        elif not accepted:
            unmatched.append(record)
    return TransferReport(
        source_service=plan.source_service,
        target_service=plan.target_service,
        source_playlist_id=plan.source_playlist_id,
        playlist_name=plan.playlist_name,
        confidence_threshold=plan.confidence_threshold,
        duration_tolerance_ms=plan.duration_tolerance_ms,
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
    duration_tolerance_ms: int,
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
        duration_tolerance_ms=duration_tolerance_ms,
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
