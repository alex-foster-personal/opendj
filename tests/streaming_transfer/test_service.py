"""Live execute_transfer regressions for META-05 target playlist ordering."""

from __future__ import annotations

from dataclasses import replace

import pytest

from apps.spotify.client import parse_playlist_payload
from apps.streaming_transfer.llm import LlmDecision, batch_id_for
from apps.streaming_transfer.service import execute_transfer, plan_transfer
from apps.streaming_transfer.soundcloud import parse_tracks_payload
from tests.streaming_transfer.test_transfer import (
    SOUNDCLOUD_STUDIO_WITH_ISRC,
    SOUNDCLOUD_STUDIO_WITHOUT_ISRC,
    SPOTIFY_CAPTURE,
    _FixedLlm,
    _RecordingTarget,
)


@pytest.mark.requirement("META-05")
def test_live_playlist_omits_unmatched_between_matches_keeps_source_order() -> None:
    """[if] unmatched sits between matches [then] playlist keeps source order, [else stop]."""
    source_capture = parse_playlist_payload(SPOTIFY_CAPTURE)
    t0, t1, t3 = source_capture.tracks[0], source_capture.tracks[1], source_capture.tracks[3]
    source = replace(source_capture, tracks=(t0, t3, t1))
    target_candidates = {
        t0.spotify_uri: parse_tracks_payload(SOUNDCLOUD_STUDIO_WITH_ISRC),
        t3.spotify_uri: (),
        t1.spotify_uri: (
            replace(
                parse_tracks_payload(SOUNDCLOUD_STUDIO_WITHOUT_ISRC)[0],
                urn="soundcloud:tracks:718846079",
            ),
        ),
    }
    plan = plan_transfer(
        source,
        target_service="soundcloud",
        candidates=target_candidates,
        confidence_threshold=0.70,
        duration_tolerance_ms=2_000,
    )
    batch_id = batch_id_for(plan.escalations)
    llm = _FixedLlm(
        (
            LlmDecision(
                source_id="spotify:track:6qYkmqFsXbj8CQjAdbYz07",
                target_id="soundcloud:tracks:718846079",
                confidence=0.99,
                evidence_urls=(
                    "https://open.spotify.com/track/6qYkmqFsXbj8CQjAdbYz07",
                    "https://soundcloud.com/theweeknd/blinding-lights",
                ),
                web_search_batch_id=batch_id,
                web_search_requests=1,
            ),
        )
    )
    target = _RecordingTarget(target_candidates)
    report = execute_transfer(
        source,
        target=target,
        llm=llm,
        confidence_threshold=0.70,
        duration_tolerance_ms=2_000,
        live=True,
        sharing="private",
    )
    assert [track.urn for track in target.created[0]] == [
        "soundcloud:tracks:718846078",
        "soundcloud:tracks:718846079",
    ]
    assert len(target.created[0]) == 2
    assert report.target_playlist_id == "soundcloud:playlists:1"
    assert len(report.matched) == 2
    matched_urns = {row.target_uri for row in report.matched}
    assert t3.spotify_uri not in {row.source_uri for row in report.matched}
    assert "soundcloud:tracks:718846078" in matched_urns
    assert "soundcloud:tracks:718846079" in matched_urns
