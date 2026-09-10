"""SET-06a: metadata-only SoundCloud tracklist from the SET-01 timeline."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.sets.manifest import Manifest, write_manifest
from apps.sets.paths import SessionPathError
from apps.sets.soundcloud_export import (
    LICENSING_REMINDER,
    SessionNotFound,
    build_soundcloud_export,
    format_soundcloud_timestamp,
)


def _seed_session(
    sets_root: Path,
    session_id: str = "s1",
    timeline: list[dict[str, Any]] | None = None,
) -> Path:
    sess = sets_root / session_id
    sess.mkdir(parents=True, exist_ok=True)
    write_manifest(
        sess,
        Manifest(
            session_id=session_id,
            started_at="2026-04-17T21:30:00+00:00",
            ended_at="2026-04-17T23:00:00+00:00",
            capture_device="BlackHole 2ch",
            event_count=len(timeline or []),
            deck_sources=["djay_monitor"],
        ),
    )
    if timeline is not None:
        (sess / "timeline.jsonl").write_text(
            "\n".join(json.dumps(row) for row in timeline) + "\n",
            encoding="utf-8",
        )
    return sess


def _play(
    session_id: str,
    timestamp_s: float,
    *,
    title: str | None = None,
    artist: str | None = None,
    track_stable_id: str | None = "uuid-1",
    source: str = "djay_monitor",
    deck: str | None = "A",
    extra_value: dict[str, Any] | None = None,
    action: str = "track_loaded",
) -> dict[str, Any]:
    value: dict = {}
    if extra_value:
        value.update(extra_value)
    if title is not None:
        value["title"] = title
    if artist is not None:
        value["artist"] = artist
    return {
        "session_id": session_id,
        "timestamp_s": timestamp_s,
        "wall_clock": "2026-04-17T21:30:00+00:00",
        "action": action,
        "source": source,
        "deck": deck,
        "track_stable_id": track_stable_id,
        "value": value,
    }


THREE_TRACK_TIMELINE = [
    _play(
        "s1",
        0.0,
        action="session_start",
        title=None,
        artist=None,
        track_stable_id=None,
        source="recorder",
        deck=None,
    ),
    _play("s1", 0.0, title="First Tune", artist="Artist One", track_stable_id="uuid-1"),
    _play("s1", 10.0, title="First Tune", artist="Artist One", track_stable_id="uuid-1"),
    _play(
        "s1",
        60.0,
        action="heartbeat",
        title=None,
        artist=None,
        track_stable_id=None,
        source="recorder",
        deck=None,
    ),
    _play(
        "s1",
        180.0,
        action="track_change",
        title="Second Tune",
        artist="Artist Two",
        track_stable_id="uuid-2",
        deck="B",
    ),
    _play(
        "s1",
        185.0,
        title="Second Tune",
        artist="Artist Two",
        track_stable_id="uuid-2",
        deck="B",
    ),
    _play("s1", 3725.0, title="Third Tune", artist="Artist Three", track_stable_id="uuid-3"),
]


@pytest.mark.requirement("SET-06a")
def test_timestamp_labels_match_soundcloud_clickable_format() -> None:
    assert format_soundcloud_timestamp(0) == "0:00"
    assert format_soundcloud_timestamp(185) == "3:05"
    assert format_soundcloud_timestamp(3599) == "59:59"
    assert format_soundcloud_timestamp(3600) == "1:00:00"
    assert format_soundcloud_timestamp(3725) == "1:02:05"


@pytest.mark.requirement("SET-06a")
def test_three_track_loaded_rows_become_timestamped_comment(sets_root: Path) -> None:
    _seed_session(sets_root, timeline=THREE_TRACK_TIMELINE)
    export = build_soundcloud_export("s1", sets_root=sets_root)

    assert [row.timestamp_label for row in export.tracklist] == ["0:00", "3:05", "1:02:05"]
    assert [row.display_name for row in export.tracklist] == [
        "Artist One - First Tune",
        "Artist Two - Second Tune",
        "Artist Three - Third Tune",
    ]
    assert export.comment == (
        "0:00 Artist One - First Tune\n"
        "3:05 Artist Two - Second Tune\n"
        "1:02:05 Artist Three - Third Tune\n"
    )
    assert "First Tune" in export.comment
    assert "Second Tune" in export.comment
    assert "Third Tune" in export.comment


@pytest.mark.requirement("SET-06a")
def test_consecutive_duplicate_stable_id_collapses_to_first_timestamp(sets_root: Path) -> None:
    _seed_session(sets_root, timeline=THREE_TRACK_TIMELINE)
    export = build_soundcloud_export("s1", sets_root=sets_root)
    assert [row.track_stable_id for row in export.tracklist] == ["uuid-1", "uuid-2", "uuid-3"]
    assert export.tracklist[0].timestamp_s == 0.0


@pytest.mark.requirement("SET-06a")
def test_heartbeat_track_change_session_start_are_not_rows(sets_root: Path) -> None:
    _seed_session(sets_root, timeline=THREE_TRACK_TIMELINE)
    export = build_soundcloud_export("s1", sets_root=sets_root)
    assert len(export.tracklist) == 3
    assert all("heartbeat" not in row.display_name for row in export.tracklist)


@pytest.mark.requirement("SET-06a")
def test_rekordbox_row_without_title_is_unresolved_not_invented(sets_root: Path) -> None:
    timeline = [
        _play(
            "s1",
            12.0,
            title=None,
            artist=None,
            track_stable_id="c-1",
            source="rb_history",
            deck=None,
            extra_value={
                "playlist_id": "hist-1",
                "note": "RB HISTORY writes on track END; event is late",
            },
        )
    ]
    _seed_session(sets_root, timeline=timeline)
    export = build_soundcloud_export("s1", sets_root=sets_root)
    assert len(export.tracklist) == 1
    assert export.tracklist[0].title is None
    assert export.tracklist[0].artist is None
    assert export.tracklist[0].display_name == "unresolved (c-1)"
    assert "Unknown" not in export.comment
    assert export.comment == "0:12 unresolved (c-1)\n"


@pytest.mark.requirement("SET-06a")
def test_null_id_with_title_is_kept(sets_root: Path) -> None:
    timeline = [
        _play(
            "s1",
            5.0,
            title="Audible Play",
            artist="Someone",
            track_stable_id=None,
            source="opendj_decks",
            deck="1",
        )
    ]
    _seed_session(sets_root, timeline=timeline)
    export = build_soundcloud_export("s1", sets_root=sets_root)
    assert len(export.tracklist) == 1
    assert export.tracklist[0].display_name == "Someone - Audible Play"


@pytest.mark.requirement("SET-06a")
def test_empty_timeline_is_empty_export(sets_root: Path) -> None:
    _seed_session(sets_root, timeline=[])
    export = build_soundcloud_export("s1", sets_root=sets_root)
    assert export.tracklist == []
    assert export.comment == ""


@pytest.mark.requirement("SET-06a")
def test_missing_timeline_file_is_empty_export(sets_root: Path) -> None:
    _seed_session(sets_root, timeline=None)
    export = build_soundcloud_export("s1", sets_root=sets_root)
    assert export.tracklist == []
    assert export.comment == ""


@pytest.mark.requirement("SET-06a")
def test_missing_session_raises_named_error(sets_root: Path) -> None:
    with pytest.raises(SessionNotFound):
        build_soundcloud_export("nope", sets_root=sets_root)


@pytest.mark.requirement("SET-06a")
def test_path_traversal_session_id_raises_session_path_error(sets_root: Path) -> None:
    with pytest.raises(SessionPathError):
        build_soundcloud_export("..\\outside", sets_root=sets_root)


@pytest.mark.requirement("SET-06a")
def test_payload_sentinels_lock_the_rights_gate(sets_root: Path) -> None:
    _seed_session(sets_root, timeline=THREE_TRACK_TIMELINE)
    export = build_soundcloud_export("s1", sets_root=sets_root)
    payload = export.to_dict()
    assert payload["kind"] == "metadata_only"
    assert payload["audio_upload"] == "not_offered"
    assert payload["takeover"] == "not_offered"
    assert payload["rights_position"] == "unsettled"
    assert "upload_url" not in payload
    assert "player_url" not in payload
    assert "takeover_url" not in payload
    assert payload["licensing_reminder"] is LICENSING_REMINDER
    assert LICENSING_REMINDER
    assert "does not upload audio" in LICENSING_REMINDER
    reminder_key_order = list(payload.keys())
    assert reminder_key_order.index("licensing_reminder") < reminder_key_order.index("comment")
