"""Stems evicted to R2 are done, not "needs the farm" (HEALTH-07).

Regression lines:
  - if a bundle evicted to R2 and fetchable here counts as pending then broken
  - if a track with no local bundle and no index entry counts as done then broken
  - if an unreadable, missing or unarmed index counts anything as in cloud then broken
  - if a machine with no cloud at all reads as unknown instead of pending then broken
  - if an index entry without a manifest counts as in cloud then broken
  - if local + in_cloud ever differs from done then broken
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.cloud import stem_index
from apps.webui.server import coverage_cloud
from apps.webui.server.routes import ingest as ingest_mod
from tests.cloudsync.conftest import InMemoryAssetS3
from tests.health_lights import fixtures as fx
from tests.health_lights.conftest import Library

pytestmark = pytest.mark.requirement("HEALTH-07")


@pytest.fixture
def s3() -> InMemoryAssetS3:
    return InMemoryAssetS3()


@pytest.fixture
def cfg():
    from apps.cloud.config import CloudConfig

    return CloudConfig(
        r2_account_id="acct1234", r2_access_key_id="AKIDEXAMPLE",
        r2_secret_access_key="secret-key", state_bucket="test-state",
        audio_bucket="test-audio", hostname="test-host", bind_host="127.0.0.1",
    )


def _present(library: Library, stable_id: str, *, stems: bool = False) -> Path:
    audio = fx.audio_file(library.music, f"{stable_id}.mp3")
    fx.seed_track(library.state_db, stable_id, str(audio))
    if stems:
        fx.write_stem_bundle(library.stems, stable_id)
    return audio


def _coverage(library: Library) -> dict:
    response = library.client.get("/api/v1/ingest/coverage")
    assert response.status_code == 200, response.text
    return response.json()


def _three_tracks(library: Library, s3, cfg, tmp_path: Path) -> dict[str, dict[str, str]]:
    """local: bundle on disk. cloud: evicted, in the index. neither: nowhere."""
    _present(library, "local", stems=True)
    _present(library, "cloud")
    _present(library, "neither")
    return {"cloud": fx.put_in_cloud(s3, cfg, tmp_path / "scratch", "cloud")}


def test_evicted_bundle_in_the_index_is_done_and_counted_as_in_cloud(
    library: Library, s3, cfg, tmp_path: Path
) -> None:
    index = _three_tracks(library, s3, cfg, tmp_path)
    fx.arm_cloud(library.app, library.data_dir, s3, cfg, index)

    body = _coverage(library)

    assert body["on_disk"] == 3
    assert body["done"]["stems"] == 2
    assert body["local"] == {"stems": 1}
    assert body["in_cloud"] == {"stems": 1}
    assert body["stems_index"] == {"state": "ok", "reason": None}
    # Overshoot control: the track in NEITHER place is still pending. A fix
    # that counted every missing bundle as in cloud would read 0 here.
    assert body["pending"]["stems"] == 1
    snapshot = ingest_mod.build_snapshot(library.app)
    assert [sid for sid, _path in snapshot.pending["stems"]] == ["neither"]


def test_local_plus_in_cloud_always_equals_done(library: Library, s3, cfg, tmp_path: Path) -> None:
    index = _three_tracks(library, s3, cfg, tmp_path)
    # A bundle that is BOTH local and indexed is local, counted once.
    index["local"] = fx.put_in_cloud(s3, cfg, tmp_path / "scratch", "local")
    fx.arm_cloud(library.app, library.data_dir, s3, cfg, index)

    body = _coverage(library)

    assert body["local"]["stems"] == 1 and body["in_cloud"]["stems"] == 1
    assert body["local"]["stems"] + body["in_cloud"]["stems"] == body["done"]["stems"]
    for step in ("stems", "vocals", "lyrics"):
        total = sum(body[state][step] for state in ("done", "terminal", "failed", "pending"))
        assert total == body["on_disk"], step


def test_no_cloud_on_this_machine_is_off_and_missing_bundles_are_pending(
    library: Library, s3, cfg, tmp_path: Path
) -> None:
    """The pre-existing behavior, kept: with no hydration source nothing is
    fetchable, so an index file lying around must not make anything done."""
    index = _three_tracks(library, s3, cfg, tmp_path)
    stem_index.save_cached_index(library.data_dir, index)   # present, but no source armed

    body = _coverage(library)

    assert body["stems_index"]["state"] == "off"
    assert body["in_cloud"] == {"stems": 0}
    assert body["pending"]["stems"] == 2


@pytest.mark.parametrize("damage", ["corrupt", "missing", "unarmed"])
def test_unreadable_index_is_unknown_and_counts_nothing_as_done(
    library: Library, s3, cfg, tmp_path: Path, damage: str
) -> None:
    index = _three_tracks(library, s3, cfg, tmp_path)
    fx.arm_cloud(library.app, library.data_dir, s3, cfg, index)
    cache = stem_index.local_index_cache_path(library.data_dir)
    # Positive control first: this exact library DOES read one in cloud, so
    # the zero below is the damage and not a fixture that never had any.
    assert _coverage(library)["in_cloud"] == {"stems": 1}
    if damage == "corrupt":
        cache.write_text("{ not json", encoding="utf-8")
    elif damage == "missing":
        cache.unlink()
    elif damage == "unarmed":
        library.app.state.stem_hydration_source = None
        library.app.state.stem_hydration_unarmed_reason = "hub enrollment expired"

    body = _coverage(library)

    assert body["stems_index"]["state"] == "unknown"
    assert body["stems_index"]["reason"]
    assert body["in_cloud"] == {"stems": 0}
    assert body["done"]["stems"] == 1          # the local bundle only
    assert body["pending"]["stems"] == 2       # never promoted to done


def test_index_entry_without_a_manifest_is_not_fetchable_so_not_done(
    library: Library, s3, cfg, tmp_path: Path
) -> None:
    index = _three_tracks(library, s3, cfg, tmp_path)
    index["neither"] = {"vocals.wav": "b" * 64}
    fx.arm_cloud(library.app, library.data_dir, s3, cfg, index)

    assert _coverage(library)["in_cloud"] == {"stems": 1}
    assert _coverage(library)["pending"]["stems"] == 1


def test_stem_cloud_holds_only_when_state_is_ok() -> None:
    entry = {"x": {"manifest.json": "a" * 64}}
    assert coverage_cloud.StemCloud("ok", None, entry).holds("x") is True
    assert coverage_cloud.StemCloud("ok", None, entry).holds("y") is False
    assert coverage_cloud.StemCloud("unknown", "why", entry).holds("x") is False
    assert coverage_cloud.StemCloud("off", None, entry).holds("x") is False


def test_index_memo_rereads_when_the_file_changes(tmp_path: Path) -> None:
    """[if] the cache file is rewritten [then] the memo serves the new index."""
    stem_index.save_cached_index(tmp_path, {"a": {"manifest.json": "a" * 64}})
    first = stem_index.load_cached_index_memo(tmp_path)
    assert stem_index.load_cached_index_memo(tmp_path) is first      # memo hit, no re-parse
    stem_index.save_cached_index(
        tmp_path, {"a": {"manifest.json": "a" * 64}, "b": {"manifest.json": "b" * 64}}
    )
    assert set(stem_index.load_cached_index_memo(tmp_path)) == {"a", "b"}


def test_vocals_split_download_from_farm(library: Library, s3, cfg, tmp_path: Path) -> None:
    """[if] a vocals-pending track's bundle is in cloud [then] it awaits a
    download, and only the track with no bundle anywhere waits on stems."""
    index = _three_tracks(library, s3, cfg, tmp_path)
    fx.arm_cloud(library.app, library.data_dir, s3, cfg, index)

    body = _coverage(library)

    assert body["pending"]["vocals"] == 3
    assert body["awaiting_stem_download"] == 1
    assert body["waiting_on_stems"] == 1
