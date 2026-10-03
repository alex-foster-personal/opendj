"""Named stem state and on-demand hydrate trigger for one track (STEM-44..46).

The deck needs to tell five different situations apart, and so does an agent:
the bundle is here, it is only in the cloud, it is being fetched (with
progress), the last fetch failed (with the reason, retryable), or there is no
bundle anywhere. Before this, the only read was the manifest GET, which also
STARTED a download as a side effect and reported a failed fetch forever.

* [if] a bundle is on local disk [then] the state is ``local``.
* [if] a bundle is only in the cloud index [then] the state is ``cloud`` and
  reading it does NOT start a download.
* [if] no bundle exists locally or in the index [then] the state is ``none``.
* [if] a fetch is in flight [then] the state is ``fetching`` with file and byte
  progress, and the manifest envelope carries the same progress.
* [if] a fetch failed [then] the state is ``error`` with the code and message,
  and ``POST .../stems/hydrate`` clears it and tries again.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

import apps.webui.server.routes.stems as stems_module
from apps.cloud import stem_hydration
from apps.cloud.stem_index import load_cached_index, save_cached_index
from tests.cloudsync.conftest import InMemoryAssetS3
from tests.webui.test_stems_hydration import (  # noqa: F401 - fixture import
    _cfg,
    _client,
    _manifest_bytes,
    _reset_hydration_module_state,
    _seed_bundle,
    _wav_bytes,
)

SID = "remote-track"


def _write_local_bundle(stems_dir: Path, stable_id: str) -> None:
    bundle = stems_dir / stable_id
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_bytes(_manifest_bytes(stable_id))
    for part in ("vocals", "drums", "bass", "other"):
        (bundle / f"{part}.wav").write_bytes(_wav_bytes())


@pytest.mark.requirement("STEM-44")
def test_state_is_local_for_a_bundle_on_disk(tmp_path: Path):
    """[if] the bundle is on disk [then] state is local with no progress, [else stop]."""
    stems_dir = tmp_path / "stems"
    _write_local_bundle(stems_dir, SID)
    with _client(stems_dir, data_dir=tmp_path / "data") as client:
        resp = client.get(f"/api/v1/tracks/{SID}/stems/state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "local"
    assert body["progress"] is None
    assert body["error_code"] is None


@pytest.mark.requirement("STEM-44")
def test_state_is_cloud_and_reading_it_starts_no_download(tmp_path: Path):
    """[if] the bundle is cloud-only [then] state is cloud and nothing is fetched, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.get(f"/api/v1/tracks/{SID}/stems/state")
    assert resp.status_code == 200
    assert resp.json()["state"] == "cloud"
    assert resp.json()["hydration_armed"] is True
    # Presence of the good thing, not absence of a bad one: the registry the
    # manifest route fills when it DOES enqueue is the thing that must be empty,
    # and the control below shows that same registry fills on a real enqueue.
    assert SID not in stems_module._INFLIGHT
    assert not (stems_dir / SID).exists()


@pytest.mark.requirement("STEM-44")
def test_state_is_none_when_no_bundle_exists_anywhere(tmp_path: Path):
    """[if] no bundle is local or indexed [then] state is none, never a 502, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {})
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        armed = client.get("/api/v1/tracks/truly-nowhere/stems/state")
    with _client(stems_dir, data_dir=data_dir) as client:
        unarmed = client.get("/api/v1/tracks/truly-nowhere/stems/state")
    assert armed.status_code == 200
    assert armed.json()["state"] == "none"
    assert armed.json()["hydration_armed"] is True
    assert unarmed.status_code == 200
    assert unarmed.json()["state"] == "none"
    assert unarmed.json()["hydration_armed"] is False


class _SlowAfterTwoS3(InMemoryAssetS3):
    """The in-memory object store, with the third download held open.

    A real fetch is sequential, so holding the third GET leaves the first two
    files written to the fetch's real temp directory by the real hydration
    path, the state a progress read sees during a slow download.
    """

    def __init__(self) -> None:
        super().__init__()
        self.gets = 0
        self.held = threading.Event()
        self.release = threading.Event()

    def get_object(self, bucket: str, key: str) -> tuple[bytes, str] | None:
        self.gets += 1
        if self.gets == 3:
            self.held.set()
            assert self.release.wait(timeout=20), "test never released the held download"
        return super().get_object(bucket, key)


@pytest.mark.requirement("STEM-45")
def test_fetching_state_reports_progress_from_the_in_flight_download(tmp_path: Path):
    """[if] a fetch is in flight [then] state and manifest both carry progress, [else stop].

    The real hydration runs in the route's own executor against the in-memory
    object store; only the store's third download is held, so the read happens
    while the download is genuinely in flight with real bytes in its real temp
    directory.
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = _SlowAfterTwoS3()
    entry = _seed_bundle(s3, cfg, SID)
    save_cached_index(data_dir, {SID: entry})
    # The fetch walks the index in its stored order; the first two files of
    # that order are the ones on disk when the third download is held.
    sizes = {name: len(_wav_bytes()) for name in entry}
    sizes["manifest.json"] = len(_manifest_bytes(SID))
    written = sum(sizes[name] for name in list(load_cached_index(data_dir)[SID])[:2])

    try:
        with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
            first = client.get(f"/api/v1/tracks/{SID}/stems")
            assert first.json()["hydrating"] is True
            assert s3.held.wait(timeout=20), "the download never reached its third file"
            state = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
            manifest = client.get(f"/api/v1/tracks/{SID}/stems").json()
            s3.release.set()
            stems_module._INFLIGHT[SID].result(timeout=20)
            done = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    finally:
        s3.release.set()

    assert state["state"] == "fetching"
    assert state["progress"] == {
        "files_total": len(entry),
        "files_done": 2,
        "bytes_done": written,
    }
    assert manifest["hydrating"] is True
    assert manifest["progress"] == state["progress"]
    assert done["state"] == "local"
    assert done["progress"] is None


@pytest.mark.requirement("STEM-45")
def test_progress_read_racing_a_finished_fetch_counts_nothing(tmp_path: Path):
    """[if] the fetch publishes its temp directory as progress is read [then] the read counts it as nothing, never an error, [else stop]."""
    gone = tmp_path / f"{SID}{stem_hydration.IN_FLIGHT_MARKER}published"
    assert stems_module._written_so_far(gone) == (0, 0)
    gone.mkdir()
    (gone / "manifest.json").write_bytes(b"x" * 10)
    (gone / "vocals.wav").write_bytes(b"x" * 30)
    assert stems_module._written_so_far(gone) == (2, 40)


@pytest.mark.requirement("STEM-46")
def test_failed_fetch_is_a_named_error_and_hydrate_retries_it(tmp_path: Path):
    """[if] a fetch failed [then] state is error; POST hydrate retries and lands it, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    good = _seed_bundle(s3, cfg, SID)
    broken = {**good, "vocals.wav": "0" * 64}  # a hash that is not in the store
    save_cached_index(data_dir, {SID: broken})

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        client.get(f"/api/v1/tracks/{SID}/stems")
        stems_module._INFLIGHT[SID].result(timeout=20)
        failed = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        still_failed = client.get(f"/api/v1/tracks/{SID}/stems")

        # The cloud copy is repaired; without a retry the recorded failure
        # would keep answering 502 for the life of the engine.
        save_cached_index(data_dir, {SID: good})
        retried = client.post(f"/api/v1/tracks/{SID}/stems/hydrate")
        stems_module._INFLIGHT[SID].result(timeout=20)
        landed = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        manifest = client.get(f"/api/v1/tracks/{SID}/stems")

    assert failed["state"] == "error"
    assert failed["error_code"] == "STEM_BUNDLE_HYDRATION_FAILED"
    assert failed["message"]
    assert still_failed.status_code == 502
    assert retried.status_code == 200
    assert retried.json()["state"] in ("fetching", "local")
    assert landed["state"] == "local"
    assert manifest.status_code == 200
    assert manifest.json()["stable_id"] == SID
    assert "parts" in manifest.json()


@pytest.mark.requirement("STEM-46")
def test_hydrate_replaces_an_invalid_local_bundle_with_the_cloud_copy(tmp_path: Path):
    """[if] the local bundle is invalid and the cloud has one [then] hydrate lands it, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        broken = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        retried = client.post(f"/api/v1/tracks/{SID}/stems/hydrate")
        stems_module._INFLIGHT[SID].result(timeout=20)
        landed = client.get(f"/api/v1/tracks/{SID}/stems/state").json()

    assert broken["state"] == "error"
    assert broken["error_code"] == "STEM_ARTIFACT_INVALID"
    assert retried.status_code == 200
    assert retried.json()["state"] in ("fetching", "local")
    assert landed["state"] == "local"


@pytest.mark.requirement("STEM-46")
def test_hydrate_on_an_invalid_bundle_with_no_cloud_copy_names_the_bundle(tmp_path: Path):
    """[if] the local bundle is invalid and the cloud has none [then] hydrate is a 422, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    save_cached_index(data_dir, {})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=_cfg(), hydration_s3=InMemoryAssetS3()) as client:
        resp = client.post(f"/api/v1/tracks/{SID}/stems/hydrate")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "STEM_ARTIFACT_INVALID"


@pytest.mark.requirement("STEM-45")
def test_unarmed_state_is_none_only_when_the_cached_index_lacks_the_track(tmp_path: Path):
    """[if] hydration failed to arm and the cached index lacks the track [then] none; with no cache, error, [else stop]."""
    stems_dir = tmp_path / "stems"
    indexed = tmp_path / "indexed"
    save_cached_index(indexed, {"someone-else": {}})
    reason = "cloud stems are configured but the R2 credentials are missing"
    with _client(stems_dir, data_dir=indexed) as client:
        client.app.state.stem_hydration_unarmed_reason = reason
        absent = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    with _client(stems_dir, data_dir=tmp_path / "never-fetched") as client:
        client.app.state.stem_hydration_unarmed_reason = reason
        unknown = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    assert absent["state"] == "none"
    assert absent["hydration_armed"] is False
    # Control: with no cached index nothing proves the track has no stems.
    assert unknown["state"] == "error"
    assert unknown["error_code"] == "STEM_HYDRATION_NOT_ARMED"
    assert unknown["message"] == reason


@pytest.mark.requirement("STEM-46")
def test_hydrate_on_a_local_bundle_is_a_no_op(tmp_path: Path):
    """[if] the bundle is already local [then] POST hydrate fetches nothing, [else stop]."""
    stems_dir = tmp_path / "stems"
    _write_local_bundle(stems_dir, SID)
    with _client(stems_dir, data_dir=tmp_path / "data") as client:
        resp = client.post(f"/api/v1/tracks/{SID}/stems/hydrate")
    assert resp.status_code == 200
    assert resp.json()["state"] == "local"
    assert SID not in stems_module._INFLIGHT


@pytest.mark.requirement("STEM-44")
def test_state_route_is_not_swallowed_by_the_part_route(tmp_path: Path):
    """[if] /stems/state is requested [then] it is never served as a stem part, [else stop].

    ``/{stable_id}/stems/{part}`` matches the same path; registration order is
    the only thing keeping ``state`` out of it. A swallowed route answers 404
    STEM_PART_NOT_FOUND for a local bundle, which is what this asserts against.
    """
    stems_dir = tmp_path / "stems"
    _write_local_bundle(stems_dir, SID)
    with _client(stems_dir, data_dir=tmp_path / "data") as client:
        state = client.get(f"/api/v1/tracks/{SID}/stems/state")
        part = client.get(f"/api/v1/tracks/{SID}/stems/not-a-part")
    assert state.status_code == 200
    assert state.json()["stable_id"] == SID
    assert part.status_code == 404
    assert part.json()["detail"]["code"] == "STEM_PART_NOT_FOUND"
