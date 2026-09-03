"""META-05 acceptance coverage over captured provider metadata.

The Spotify rows were captured from the official Web API search endpoint on
Thu 3 Sep 2026. The SoundCloud rows are the matching public track hydration
records captured on the same day, limited to fields in SoundCloud's documented
Track schema. No provider implementation is replaced in this test: the same
parsers, scorer, planner, LLM decision validator, and report builder are used by
the production HTTP orchestration.

- if deterministic and escalated matches lose confidence then META-05 is broken
- if a below-threshold candidate bypasses the batched LLM pass then META-05 is broken
- if matched, unmatched, and ungradable are not separate buckets then META-05 is broken
"""

from __future__ import annotations

import pytest

from apps.spotify.client import parse_playlist_payload
from apps.streaming_transfer.llm import LlmDecision, build_batch_request
from apps.streaming_transfer.service import plan_transfer, resolve_transfer
from apps.streaming_transfer.soundcloud import parse_tracks_payload

SPOTIFY_CAPTURE = {
    "id": "captured-search",
    "name": "Captured Spotify search results",
    "snapshot_id": "capture-2026-09-03",
    "owner": {"id": "open-dj", "display_name": "Open DJ"},
    "description": "Read-only API capture for META-05",
    "_items": [
        {
            "track": {
                "id": "0VjIjW4GlUZAMYd2vXMi3b",
                "uri": "spotify:track:0VjIjW4GlUZAMYd2vXMi3b",
                "external_ids": {"isrc": "USUG11904206"},
                "name": "Blinding Lights",
                "artists": [{"name": "The Weeknd"}],
                "album": {"name": "After Hours"},
                "duration_ms": 200040,
                "is_local": False,
            }
        },
        {
            "track": {
                "id": "6qYkmqFsXbj8CQjAdbYz07",
                "uri": "spotify:track:6qYkmqFsXbj8CQjAdbYz07",
                "external_ids": {"isrc": "USUG11904206"},
                "name": "Blinding Lights",
                "artists": [{"name": "The Weeknd"}],
                "album": {"name": "Blinding Lights"},
                "duration_ms": 200045,
                "is_local": False,
            }
        },
        {
            "track": {
                "id": "5OGxg1400HmCYVRHuccZWw",
                "uri": "spotify:track:5OGxg1400HmCYVRHuccZWw",
                "external_ids": {"isrc": "USUG12301431"},
                "name": "Blinding Lights - Live",
                "artists": [{"name": "The Weeknd"}],
                "album": {"name": "Live At SoFi Stadium"},
                "duration_ms": 253365,
                "is_local": False,
            }
        },
        {
            "track": {
                "id": "2D4dV2KXDTszzJ3p3cFqhA",
                "uri": "spotify:track:2D4dV2KXDTszzJ3p3cFqhA",
                "external_ids": {"isrc": "USUG12106698"},
                "name": "Less Than Zero",
                "artists": [{"name": "The Weeknd"}],
                "album": {"name": "Dawn FM"},
                "duration_ms": 211814,
                "is_local": False,
            }
        },
    ],
}

SOUNDCLOUD_STUDIO_WITH_ISRC = {
    "collection": [
        {
            "kind": "track",
            "title": "Blinding Lights",
            "duration": 201619,
            "urn": "soundcloud:tracks:718846078",
            "isrc": "USUG11904206",
            "metadata_artist": "The Weeknd",
            "permalink_url": "https://soundcloud.com/theweeknd/blinding-lights",
            "access": "playable",
            "streamable": True,
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
            "kind": "track",
            "title": "Blinding Lights (Live)",
            "duration": 30000,
            "urn": "soundcloud:tracks:1455360826",
            "isrc": None,
            "metadata_artist": "The Weeknd",
            "permalink_url": ("https://soundcloud.com/theweeknd/the-weeknd-blinding-lights-1"),
            "access": "preview",
            "streamable": True,
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
    assert request["tools"] == [{"type": "openrouter:web_search"}]
    assert len(request["messages"]) == 2
    assert "duration_ms" in request["messages"][1]["content"]
    assert "Track length verification is mandatory" in request["messages"][0]["content"]

    report = resolve_transfer(
        plan,
        decisions=(
            LlmDecision(
                source_id="spotify:track:6qYkmqFsXbj8CQjAdbYz07",
                target_id="soundcloud:tracks:718846078",
                confidence=0.99,
                length_verified=True,
                evidence_urls=(
                    "https://open.spotify.com/track/6qYkmqFsXbj8CQjAdbYz07",
                    "https://soundcloud.com/theweeknd/blinding-lights",
                ),
            ),
            LlmDecision(
                source_id="spotify:track:5OGxg1400HmCYVRHuccZWw",
                target_id=None,
                confidence=0.0,
                length_verified=False,
                evidence_urls=(
                    "https://open.spotify.com/track/5OGxg1400HmCYVRHuccZWw",
                    ("https://soundcloud.com/theweeknd/the-weeknd-blinding-lights-1"),
                ),
            ),
        ),
    )
    payload = report.as_dict()

    assert set(payload["buckets"]) == {"matched", "unmatched", "ungradable"}
    assert len(payload["buckets"]["matched"]) == 2
    assert len(payload["buckets"]["unmatched"]) == 1
    assert len(payload["buckets"]["ungradable"]) == 1
    assert all(isinstance(row["confidence"], float) for row in payload["buckets"]["matched"])
    assert payload["buckets"]["matched"][1]["match_pass"] == "llm_web_search"
    assert payload["buckets"]["matched"][1]["length_verified"] is True
    assert payload["buckets"]["unmatched"][0]["match_pass"] == "llm_web_search"
