"""Pure tests for setup-import progress -> library.changed translation."""

from __future__ import annotations

import pytest

from apps.engine_core.setup.library_events import on_setup_import_progress
from apps.shared.events import set_hub
from tests.engine_core.test_emit_points import RecordingHub


@pytest.fixture
def hub() -> RecordingHub:
    recorder = RecordingHub()
    set_hub(recorder)
    yield recorder
    set_hub(None)


def test_in_progress_line_publishes_nothing(hub: RecordingHub) -> None:
    on_setup_import_progress({"payload": {"mode": "folder"}}, {"progress": 0.5})
    assert hub.events == []


def test_folder_completion_publishes_tracks_only(hub: RecordingHub) -> None:
    on_setup_import_progress(
        {"payload": {"mode": "folder"}},
        {"progress": 1.0, "message": "done"},
    )
    assert hub.events == [
        ("library.changed", {"kind": "tracks", "ids": []}),
    ]


def test_rekordbox_completion_publishes_tracks_and_playlists(
    hub: RecordingHub,
) -> None:
    on_setup_import_progress({"payload": {}}, {"progress": 1.0, "message": "done"})
    assert ("library.changed", {"kind": "tracks", "ids": []}) in hub.events
    assert ("library.changed", {"kind": "playlists", "ids": []}) in hub.events


def test_explicit_rekordbox_mode_publishes_playlists(hub: RecordingHub) -> None:
    on_setup_import_progress(
        {"payload": {"mode": "rekordbox"}},
        {"progress": 1.0, "message": "done"},
    )
    assert hub.events.count(("library.changed", {"kind": "playlists", "ids": []})) == 1
