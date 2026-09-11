"""/anlz demucs vocal-cache merge: all FOUR vocals statuses.

merge_demucs_vocals is the serve-time bridge from the trickle CLI's
vocal-cache into the /anlz contract: rekordbox / no_vocals stay PVDI-
authoritative, not_analyzed consults the cache, and after the merge
not_analyzed means NEITHER source exists.

Regression one-liners:
  - if a PVDI status is ever overridden by the demucs cache then broken
  - if not_analyzed + valid cache doesn't become status demucs then broken
  - if a stale (audio_mtime changed) entry still serves regions then broken
  - if demucs regions lack start_s/end_s/intensity (PVDI render shape) then broken
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.shared import platform_paths
from apps.vocals import cache as vcache
from apps.webui.server import rb_vendor
from apps.webui.server.rb_vendor import RbContent

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]


def _content(tmp_path: Path, folder_path: str | None) -> RbContent:
    return RbContent(
        stable_id="sid001",
        vendor_id="42",
        folder_path=folder_path,
        image_path=None,
        analysis_data_path=None,
        length_s=120,
        comment=None,
        genre=None,
    )


def _payload(vocals: dict[str, Any]) -> dict[str, Any]:
    return {"stable_id": "sid001", "vocals": vocals}


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "vocal-cache"
    monkeypatch.setattr(rb_config, "VOCAL_CACHE_DIR", d)
    return d


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    f = tmp_path / "track.mp3"
    f.write_bytes(b"audio")
    return f


def _write_cache(cache_dir: Path, audio: Path) -> dict[str, Any]:
    return vcache.write_entry(
        cache_dir / "sid001.json",
        {
            "schema": vcache.VOCAL_CACHE_SCHEMA, "source": "demucs-htdemucs", "fps": 2.0,
            "duration_s": 120.0, "coverage_pct": 41.7,
            "regions": [{"start_s": 8.5, "end_s": 57.5, "confidence": 0.84}],
            "params": {"hop_s": 0.5},
        },
        audio,
    )


def test_not_analyzed_with_valid_cache_becomes_demucs(
    cache_dir: Path, audio: Path, tmp_path: Path
) -> None:
    _write_cache(cache_dir, audio)
    content = _content(tmp_path, str(audio))
    merged = rb_vendor.merge_demucs_vocals(
        _payload({"status": "not_analyzed"}), content
    )
    assert merged["vocals"] == {
        "status": "demucs",
        "fps": 2.0,
        "regions": [
            {"start_s": 8.5, "end_s": 57.5, "intensity": 3, "confidence": 0.84}
        ],
    }
    # non-vocals payload fields ride through untouched
    assert merged["stable_id"] == "sid001"


@pytest.mark.parametrize(
    "pvdi_vocals",
    [
        {"status": "rekordbox", "fps": 21.53,
         "regions": [{"start_s": 0.0, "end_s": 5.0, "intensity": 4}]},
        {"status": "no_vocals", "fps": 21.53, "regions": []},
    ],
)
def test_pvdi_statuses_never_overridden(
    cache_dir: Path, audio: Path, tmp_path: Path, pvdi_vocals: dict[str, Any]
) -> None:
    _write_cache(cache_dir, audio)  # cache exists AND is valid
    content = _content(tmp_path, str(audio))
    merged = rb_vendor.merge_demucs_vocals(_payload(dict(pvdi_vocals)), content)
    assert merged["vocals"] == pvdi_vocals, "PVDI is authoritative (SPIKE-B2 6.1)"


def test_no_cache_stays_not_analyzed(
    cache_dir: Path, audio: Path, tmp_path: Path
) -> None:
    content = _content(tmp_path, str(audio))
    merged = rb_vendor.merge_demucs_vocals(
        _payload({"status": "not_analyzed"}), content
    )
    assert merged["vocals"] == {"status": "not_analyzed"}, (
        "not_analyzed now means NEITHER source exists"
    )


def test_stale_audio_mtime_stays_not_analyzed(
    cache_dir: Path, audio: Path, tmp_path: Path
) -> None:
    _write_cache(cache_dir, audio)
    os.utime(audio, (audio.stat().st_atime, audio.stat().st_mtime + 5))
    content = _content(tmp_path, str(audio))
    merged = rb_vendor.merge_demucs_vocals(
        _payload({"status": "not_analyzed"}), content
    )
    assert merged["vocals"] == {"status": "not_analyzed"}


@pytest.mark.parametrize("folder_path", [None, "tidal:12345"])
def test_streaming_or_pathless_never_consults_cache(
    cache_dir: Path, audio: Path, tmp_path: Path, folder_path: str | None
) -> None:
    _write_cache(cache_dir, audio)
    content = _content(tmp_path, folder_path)
    merged = rb_vendor.merge_demucs_vocals(
        _payload({"status": "not_analyzed"}), content
    )
    assert merged["vocals"] == {"status": "not_analyzed"}


def test_pioneer_path_cannot_escape_share_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "share"
    root.mkdir()
    monkeypatch.setattr(platform_paths, "SHARE_ROOT", root)
    with pytest.raises(ValueError, match="unsafe rekordbox asset path"):
        rb_vendor.resolve_share_path("/PIONEER/../../outside.mp3")
