"""META-05 acceptance coverage over captured provider metadata.

Spotify search rows and SoundCloud track hydration records were captured from
official services on Thu 3 Sep 2026. Production parsers, scoring, LLM response
validation, duration checks, and reporting consume them here without API mocks.

- if deterministic and escalated matches lose confidence then META-05 is broken
- if a below-threshold candidate bypasses the batched LLM pass then META-05 is broken
- if matched, unmatched, and ungradable are not separate buckets then META-05 is broken
"""

from __future__ import annotations

import json

import pytest

from apps.spotify.client import parse_playlist_payload
from apps.streaming_transfer.llm import (
    LlmMatchError,
    batch_id_for,
    build_batch_request,
    parse_batch_response,
)
from apps.streaming_transfer.service import plan_transfer, resolve_transfer
from apps.streaming_transfer.soundcloud import build_playlist_payload, parse_tracks_payload


def _spotify_item(
    track_id: str, isrc: str, title: str, album: str, duration_ms: int
) -> dict[str, object]:
    return {
        "track": {
            "id": track_id,
            "uri": f"spotify:track:{track_id}",
            "external_ids": {"isrc": isrc},
            "name": title,
            "artists": [{"name": "The Weeknd"}],
            "album": {"name": album},
            "duration_ms": duration_ms,
            "is_local": False,
        }
    }


SPOTIFY_CAPTURE: dict[str, object] = {
    "id": "captured-search",
    "name": "Captured Spotify search results",
    "snapshot_id": "capture-2026-09-03",
    "owner": {"id": "open-dj", "display_name": "Open DJ"},
    "description": "Read-only API capture for META-05",
    "_items": [
        _spotify_item(
            "0VjIjW4GlUZAMYd2vXMi3b", "USUG11904206", "Blinding Lights", "After Hours", 200040
        ),
        _spotify_item(
            "6qYkmqFsXbj8CQjAdbYz07", "USUG11904206", "Blinding Lights", "Blinding Lights", 200045
        ),
        _spotify_item(
            "5OGxg1400HmCYVRHuccZWw",
            "USUG12301431",
            "Blinding Lights - Live",
            "Live At SoFi Stadium",
            253365,
        ),
        _spotify_item(
            "2D4dV2KXDTszzJ3p3cFqhA", "USUG12106698", "Less Than Zero", "Dawn FM", 211814
        ),
    ],
}

SOUNDCLOUD_STUDIO_WITH_ISRC = {
    "collection": [
        {
            "kind": "track", "title": "Blinding Lights", "duration": 201619,
            "urn": "soundcloud:tracks:718846078", "isrc": "USUG11904206",
            "metadata_artist": "The Weeknd", "access": "playable", "streamable": True,
            "permalink_url": "https://soundcloud.com/theweeknd/blinding-lights",
            "user": {"username": "The Weeknd"},
        }
    ]
}

SOUNDCLOUD_STUDIO_WITHOUT_ISRC = {
    "collection": [
        {
            **SOUNDCLOUD_STUDIO_WITH_ISRC["collection"][0],
            "isrc": None,
        }
    ]
}

SOUNDCLOUD_LIVE_PREVIEW = {
    "collection": [
        {
            "kind": "track", "title": "Blinding Lights (Live)", "duration": 30000,
            "urn": "soundcloud:tracks:1455360826", "isrc": None,
            "metadata_artist": "The Weeknd", "access": "preview", "streamable": True,
            "permalink_url": ("https://soundcloud.com/theweeknd/the-weeknd-blinding-lights-1"),
            "user": {"username": "The Weeknd"},
        }
    ]
}


@pytest.mark.requirement("META-05")
def test_captured_spotify_to_soundcloud_transfer_reports_every_bucket() -> None:
    source = parse_playlist_payload(SPOTIFY_CAPTURE)
    target_candidates = {
        source.tracks[0].spotify_uri: parse_tracks_payload(SOUNDCLOUD_STUDIO_WITH_ISRC),
        source.tracks[1].spotify_uri: parse_tracks_payload(SOUNDCLOUD_STUDIO_WITHOUT_ISRC),
        source.tracks[2].spotify_uri: parse_tracks_payload(SOUNDCLOUD_LIVE_PREVIEW),
        source.tracks[3].spotify_uri: (),
    }

    plan = plan_transfer(
        source,
        target_service="soundcloud",
        candidates=target_candidates,
        confidence_threshold=0.70,
        duration_tolerance_ms=2_000,
    )

    assert [row.source_uri for row in plan.deterministic_matches] == [
        "spotify:track:0VjIjW4GlUZAMYd2vXMi3b"
    ]
    # The provider captures differ by 1,579 ms, just outside CAT-01's 1,500 ms
    # duration tolerance. ISRC + title + artist therefore produce exactly 0.70.
    assert plan.deterministic_matches[0].confidence == pytest.approx(0.70)
    assert plan.deterministic_matches[0].match_pass == "deterministic"
    assert [row.source_uri for row in plan.escalations] == [
        "spotify:track:6qYkmqFsXbj8CQjAdbYz07",
        "spotify:track:5OGxg1400HmCYVRHuccZWw",
    ]
    assert [row.source_uri for row in plan.ungradable] == ["spotify:track:2D4dV2KXDTszzJ3p3cFqhA"]

    request = build_batch_request(plan.escalations, model="operator-supplied")
    batch_id = batch_id_for(plan.escalations)
    assert request["tools"] == [{"type": "openrouter:web_search"}]
    assert "duration_ms" in request["messages"][1]["content"]
    assert json.loads(request["messages"][1]["content"])["batch_id"] == batch_id
    assert "length_verified" not in json.dumps(request["response_format"])

    decision_content = json.dumps(
        {
            "batch_id": batch_id,
            "decisions": [
                {
                    "source_id": "spotify:track:6qYkmqFsXbj8CQjAdbYz07",
                    "target_id": "soundcloud:tracks:718846078",
                    "confidence": 0.99,
                    "evidence_urls": [
                        "https://open.spotify.com/track/6qYkmqFsXbj8CQjAdbYz07",
                        "https://soundcloud.com/theweeknd/blinding-lights",
                    ],
                },
                {
                    "source_id": "spotify:track:5OGxg1400HmCYVRHuccZWw",
                    "target_id": "soundcloud:tracks:1455360826",
                    "confidence": 0.99,
                    "evidence_urls": [
                        "https://open.spotify.com/track/5OGxg1400HmCYVRHuccZWw",
                        "https://soundcloud.com/theweeknd/the-weeknd-blinding-lights-1",
                    ],
                },
            ],
        }
    )
    citations = [
        {
            "type": "url_citation",
            "url_citation": {"url": url},
        }
        for url in (
            "https://open.spotify.com/track/6qYkmqFsXbj8CQjAdbYz07",
            "https://soundcloud.com/theweeknd/blinding-lights",
            "https://open.spotify.com/track/5OGxg1400HmCYVRHuccZWw",
            "https://soundcloud.com/theweeknd/the-weeknd-blinding-lights-1",
        )
    ]
    response = {
        "usage": {"server_tool_use": {"web_search_requests": 2}},
        "choices": [
            {"message": {"content": decision_content, "annotations": citations}}
        ],
    }
    with pytest.raises(LlmMatchError, match="web_search_requests"):
        parse_batch_response({"choices": response["choices"]}, expected_batch_id=batch_id)
    uncited = {
        **response,
        "choices": [{"message": {"content": decision_content, "annotations": []}}],
    }
    with pytest.raises(LlmMatchError, match="citation"):
        parse_batch_response(uncited, expected_batch_id=batch_id)
    with pytest.raises(LlmMatchError, match="batch_id"):
        parse_batch_response(response, expected_batch_id="0" * 64)
    decisions = parse_batch_response(response, expected_batch_id=batch_id)

    report = resolve_transfer(
        plan,
        decisions=decisions,
    )
    payload = report.as_dict()

    assert payload["source_playlist_id"] == "captured-search"
    assert payload["counts"] == {"matched": 2, "unmatched": 1, "ungradable": 1}
    assert set(payload["buckets"]) == {"matched", "unmatched", "ungradable"}
    assert all(isinstance(row["confidence"], float) for row in payload["buckets"]["matched"])
    llm_match = payload["buckets"]["matched"][1]
    assert llm_match["match_pass"] == "llm_web_search"
    assert llm_match["web_search_batch_id"] == batch_id
    assert llm_match["web_search_requests"] == 2
    assert llm_match["duration_delta_ms"] == 1_574
    assert llm_match["duration_tolerance_ms"] == 2_000
    assert llm_match["duration_within_tolerance"] is True
    llm_rejection = payload["buckets"]["unmatched"][0]
    assert llm_rejection["match_pass"] == "llm_web_search"
    assert llm_rejection["duration_delta_ms"] == 223_365
    assert llm_rejection["duration_within_tolerance"] is False

    target = parse_tracks_payload(SOUNDCLOUD_STUDIO_WITH_ISRC)[0]
    playlist_payload = build_playlist_payload(
        "Captured Spotify search results",
        (target, target),
        sharing="private",
    )
    assert playlist_payload["playlist"]["tracks"] == [
        {"id": 718846078},
        {"id": 718846078},
    ]
