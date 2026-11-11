"""The historical paths surface keeps its exports and call-time config overrides."""

from pathlib import Path

import pytest
from fastapi import HTTPException

from apps.adapters.rekordbox import config, paths


def test_historical_exports_remain_callable() -> None:
    expected = {
        "_asset_sibling",
        "anlz_dir",
        "artwork_file",
        "audio_file",
        "bulk_local_artwork_available",
        "empty_anlz_payload",
        "empty_hot_cue_slots",
        "is_streaming_path",
        "local_artwork",
        "local_artwork_available",
        "local_audio_file",
        "local_track_file_tags",
        "list_rekordbox_usb_mentions",
        "local_track_row",
        "resized_artwork_cache",
        "resolve_asset_path",
        "resolve_content",
        "resolve_playable_audio",
        "resolve_share_path",
    }
    assert set(paths.__all__) == expected
    for name in expected | {"_resolve_local_audio_path", "_rekordbox_copy"}:
        assert callable(getattr(paths, name)), name


def test_content_resolution_reads_rebound_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert paths.config is config
    for name in ("first.db", "second.db"):
        absent = tmp_path / name
        monkeypatch.setattr(paths.config, "STATE_DB", absent)
        with pytest.raises(HTTPException) as raised:
            paths.resolve_content("missing-track")
        assert raised.value.status_code == 500
        assert raised.value.detail == {
            "code": "STATE_DB_UNAVAILABLE",
            "message": f"required database missing on disk: {absent}",
        }
        assert not absent.exists()
