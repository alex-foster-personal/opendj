"""A lyrics run that beats the stem index to a spoke must not give up for good (LYRICS-08).

On an installed spoke the stem index cache arrives from the hub after boot. A
missing cache reads as an empty index, so before this fix every LRCLIB miss
got a ``no_source`` verdict with ``vocals_sha256=None``, which
``is_terminal_fresh`` keeps final while the sha stays ``None``: those tracks
were never asked about again.

* [if] a hub transport is configured and the stem index cache is absent
  [then] the fetch raises the retryable ``LyricsAsrFetchError`` and writes no
  verdict and no cache entry, so the next run asks again
Driven through the real ``resolve_stem_hydration_source``: cloud mode plus a
CloudSync config file naming a hub (written with the production
``write_config``; nothing is contacted) is a configured transport, local mode
is not. The ASR provider is the real one,
unconfigured, so a stray ASR call fails the test with the wrong code.

* [if] no hydration transport is configured (local mode) [then] ``no_source``
  is still recorded, as before (control: the overshoot would retry forever on
  a machine that can never have an index)
* [if] the index cache exists but lacks the track [then] ``no_source`` is
  still recorded (control: a loaded index is a real answer)

[if] the spoke has no stem index yet [then] no terminal verdict is written, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.cloud import stem_index
from apps.lyrics.asr_source import AsrLyricsProvider, LyricsAsrFetchError
from apps.lyrics.cache import cache_path
from apps.lyrics.fetch_verdicts import load_verdict
from apps.lyrics.service import LYRICS_STEM_INDEX_NOT_LOADED, LyricsFetchService, Track
from apps.sync_hub import config as sync_config
from tests.lyrics.conftest import use_cloud_mode, use_local_mode

pytestmark = pytest.mark.requirement("LYRICS-08")


class _MissLrclib:
    """LRCLIB's 404 answer; the real provider needs the public internet."""

    def fetch_synced(self, track: Track) -> str | None:
        return None


def _service(data_dir: Path) -> LyricsFetchService:
    return LyricsFetchService(
        data_dir, provider=_MissLrclib(), asr_provider=AsrLyricsProvider(data_dir)
    )


def _transport(monkeypatch: pytest.MonkeyPatch, data_dir: Path, *, configured: bool) -> None:
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(sync_config.ENDPOINT_ENV, raising=False)
    if not configured:
        use_local_mode(monkeypatch)
        return
    use_cloud_mode(monkeypatch)
    sync_config.write_config(
        data_dir,
        sync_config.CloudSyncConfig(
            enabled=True, hub_url="https://hub.example.test", machine_name="spoke-test"
        ),
    )


def test_spoke_without_index_yet_fails_retryable_and_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _transport(monkeypatch, tmp_path, configured=True)
    assert not stem_index.local_index_cache_path(tmp_path).exists()
    with pytest.raises(LyricsAsrFetchError) as excinfo:
        _service(tmp_path).fetch_or_resolve(Track("sid-early", "Artist", "Title", 180))
    assert excinfo.value.code == LYRICS_STEM_INDEX_NOT_LOADED
    assert load_verdict(tmp_path, "sid-early") is None
    assert not cache_path(tmp_path, "sid-early").exists()


def test_local_mode_without_any_transport_still_records_no_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _transport(monkeypatch, tmp_path, configured=False)
    result = _service(tmp_path).fetch_or_resolve(Track("sid-local", "Artist", "Title", 180))
    assert result.outcome == "no_source"
    verdict = load_verdict(tmp_path, "sid-local")
    assert verdict is not None and verdict.outcome == "no_source"


def test_loaded_index_without_the_track_still_records_no_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _transport(monkeypatch, tmp_path, configured=True)
    stem_index.save_cached_index(
        tmp_path, {"other-track": {"manifest.json": "a" * 64, "vocals.wav": "b" * 64}}
    )
    result = _service(tmp_path).fetch_or_resolve(Track("sid-absent", "Artist", "Title", 180))
    assert result.outcome == "no_source"
    verdict = load_verdict(tmp_path, "sid-absent")
    assert verdict is not None and verdict.outcome == "no_source"
