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

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import psutil
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apps.webui.server.routes.stems as stems_module
from apps.cloud import stem_bundles, stem_hydration
from apps.cloud.stem_index import load_cached_index, save_cached_index
from apps.cloud.stem_source import DirectR2Source, HubPresignedSource
from apps.cloud.stem_upload_queue import exclusive_file_lock
from apps.stems.artifacts import StemArtifactError, load_stem_bundle
from apps.webui.server.routes.stems import router
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


@pytest.mark.requirement("STEM-44")
def test_armed_state_before_the_index_is_fetched_is_never_none(tmp_path: Path):
    """[if] hydration is armed but no index is cached yet [then] error, never none, [else stop]."""
    stems_dir = tmp_path / "stems"
    cfg = _cfg()
    with _client(stems_dir, data_dir=tmp_path / "data", hydration_cfg=cfg, hydration_s3=InMemoryAssetS3()) as client:
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    assert body["state"] == "error"
    assert body["error_code"] == "STEM_INDEX_NOT_FETCHED"
    assert body["hydration_armed"] is True


@pytest.mark.requirement("STEM-44")
def test_deck_open_names_an_open_deck_not_a_recently_served_bundle(tmp_path: Path):
    """[if] a bundle was only served [then] deck_open is false; open then close flips it, [else stop]."""
    stems_dir = tmp_path / "stems"
    _write_local_bundle(stems_dir, SID)
    with _client(stems_dir, data_dir=tmp_path / "data") as client:
        assert client.get(f"/api/v1/tracks/{SID}/stems").status_code == 200
        served = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        client.post(f"/api/v1/tracks/{SID}/stems/deck-open")
        opened = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        client.post(f"/api/v1/tracks/{SID}/stems/deck-close")
        closed = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    assert served["deck_open"] is False
    assert opened["deck_open"] is True
    assert closed["deck_open"] is False


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
    stems_dir.mkdir(parents=True)
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = _SlowAfterTwoS3()
    entry = _seed_bundle(s3, cfg, SID)
    save_cached_index(data_dir, {SID: entry})
    tempfile.mkdtemp(
        dir=stems_dir,
        prefix=f"{SID}{stem_hydration.IN_FLIGHT_MARKER}stale-unrelated-",
    )
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
    """[if] a fetch publishes its temp dir as progress is read [then] it counts as nothing, [else stop]."""
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


class _HoldFailedBundleRead(logging.Handler):
    """Hold the thread whose ``nth`` failed read of ``stable_id``'s bundle this is.

    ``load_stem_bundle`` logs this warning after its read fails and before it
    raises, and nothing reads the bundle again before the route decides, so a
    thread held here sits exactly in the window between the two. This uses the
    logger's public handler API; no production code is replaced.
    """

    def __init__(self, stable_id: str, *, nth: int = 1) -> None:
        super().__init__(level=logging.WARNING)
        self.stable_id = stable_id
        self.reached = threading.Event()
        self.resume = threading.Event()
        self._seen = 0
        self._nth = nth
        self._lock = threading.Lock()

    def handle(self, record: logging.LogRecord) -> bool:
        # The base class holds this handler's lock around emit(); the held
        # thread would then block every other thread's warning behind it.
        self.emit(record)
        return True

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if not message.startswith("invalid stem bundle skipped"):
            return
        if f"stable_id={self.stable_id} " not in message:
            return
        with self._lock:
            self._seen += 1
            hold = self._seen == self._nth
        if hold:
            self.reached.set()
            self.resume.wait(timeout=20)


def _publish_repair(publisher: str, client: Any, data_dir: Path, stems_dir: Path, cfg: Any, s3: Any) -> None:
    """Land the cloud copy through the deck route, or through the direct
    ``hydrate_one`` call the job runner and bulk hydrate make."""
    if publisher == "route":
        retried = client.post(f"/api/v1/tracks/{SID}/stems/hydrate")
        stems_module._INFLIGHT[SID].result(timeout=20)
        assert stems_module._INFLIGHT[SID].done()
        assert retried.status_code == 200
    elif publisher == "hydrate_one":
        outcome = stem_hydration.hydrate_one(
            SID,
            data_dir=data_dir,
            source=DirectR2Source(cfg=cfg, s3=s3),
            index=load_cached_index(data_dir),
            stems_dir=stems_dir,
        )
        assert outcome.status == "hydrated", outcome
    else:
        raise AssertionError(f"unknown publisher {publisher!r}")


@pytest.mark.requirement("STEM-46")
@pytest.mark.parametrize("publisher", ["route", "hydrate_one"])
def test_a_repair_landing_during_a_state_read_reads_as_local(tmp_path: Path, publisher: str):
    """[if] a repair lands between a failed read and the decision [then] local, [else stop].

    ``publisher="hydrate_one"`` is the job runner and bulk path: no in-flight
    fetch is registered with this route for it.
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    pause = _HoldFailedBundleRead(SID)
    artifacts_log = logging.getLogger(load_stem_bundle.__module__)
    artifacts_log.addHandler(pause)
    held: dict[str, dict] = {}
    try:
        with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:

            def _read_state() -> None:
                held["body"] = client.get(f"/api/v1/tracks/{SID}/stems/state").json()

            reader = threading.Thread(target=_read_state, daemon=True)
            reader.start()
            assert pause.reached.wait(timeout=20), "state read never failed a read of the bundle"
            _publish_repair(publisher, client, data_dir, stems_dir, cfg, s3)
            pause.resume.set()
            reader.join(timeout=20)
            assert not reader.is_alive(), "held state read never finished"
    finally:
        artifacts_log.removeHandler(pause)

    assert held["body"]["state"] == "local", held["body"]


@pytest.mark.requirement("STEM-58")
def test_hydrate_repairs_an_invalid_shared_bundle_behind_the_read_barrier(tmp_path: Path):
    """[if] hydration replaces an invalid bundle in the shared store [then] it waits on the read barrier, [else stop].

    The repair used to delete the invalid bundle and rename the fetched one
    in the open, so a deck read in between saw it half deleted or missing
    (Codex on #6821). It now goes through the barrier-held swap. While a read
    holds the barrier the old bundle stays whole; once it lets go the
    repaired bundle lands.

    MUTATION TARGET: restore ``shutil.rmtree`` + ``rename`` in
    ``_publish_fetched``.
    """
    from apps.cloud.stem_cache_budget import read_barrier

    data_dir = tmp_path / "data"
    stems_dir = data_dir / "state" / "stems"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    before = sorted(p.name for p in (stems_dir / SID).iterdir())

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        with read_barrier(data_dir, SID):
            retried = client.post(f"/api/v1/tracks/{SID}/stems/hydrate")
            future = stems_module._INFLIGHT[SID]
            done = threading.Event()
            future.add_done_callback(lambda _f: done.set())
            assert not done.wait(1.0), "the repair waits for the read holding the barrier"
            assert sorted(p.name for p in (stems_dir / SID).iterdir()) == before, "the old bundle stays whole"
            assert (stems_dir / SID / "vocals.wav").read_bytes() == b"not a wav"
        future.result(timeout=20)
        landed = client.get(f"/api/v1/tracks/{SID}/stems/state").json()

    assert retried.status_code == 200
    assert landed["state"] == "local", "control: once the read lets go the repair lands"


@pytest.mark.requirement("STEM-46")
@pytest.mark.parametrize("bundle_mid_publish", ["removed", "still_invalid"])
def test_a_state_read_during_another_hydrators_publish_reads_as_fetching(
    tmp_path: Path, bundle_mid_publish: str
):
    """[if] another process holds the hydrate lock mid-publish [then] fetching, [else stop].

    A fetch error is recorded from an earlier attempt, so a read that only
    saw the gap between the publisher's rmtree and rename would answer it.
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    good = _seed_bundle(s3, cfg, SID)
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        save_cached_index(data_dir, {SID: {**good, "vocals.wav": "0" * 64}})
        client.post(f"/api/v1/tracks/{SID}/stems/hydrate")
        stems_module._INFLIGHT[SID].result(timeout=20)
        assert stems_module._recorded_hydration_error(SID) is not None, "control: no error recorded"
        save_cached_index(data_dir, {SID: good})

        # What a hydrate_runner job in another process holds mid-publish: the
        # file lock, and its live owner marker for this track.
        with exclusive_file_lock(stem_hydration.hydrate_lock_path(data_dir, SID)):
            owner = _live_owner_marker(data_dir, SID)
            if bundle_mid_publish == "removed":
                shutil.rmtree(stems_dir / SID)
            elif bundle_mid_publish != "still_invalid":
                raise AssertionError(f"unknown case {bundle_mid_publish!r}")
            started = time.monotonic()
            during = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
            waited = time.monotonic() - started
            owner.unlink()

        _publish_repair("hydrate_one", client, data_dir, stems_dir, cfg, s3)
        after = client.get(f"/api/v1/tracks/{SID}/stems/state").json()

    assert during["state"] == "fetching", during
    assert waited < stems_module.STEM_STATE_SETTLE_S, f"read waited {waited:.2f}s for its own fetch"
    assert after["state"] == "local", after


def _write_owner_marker(
    data_dir: Path,
    stable_id: str,
    *,
    pid: int,
    create_time: float,
    tag: str,
    started_at: float | None = None,
) -> Path:
    """An owner marker in the format a hydrate publishes before it takes a lock."""
    owners = stem_hydration.hydrate_owners_dir(data_dir)
    owners.mkdir(parents=True, exist_ok=True)
    marker = owners / f"{stable_id}@{pid}-{tag}.json"
    record = {
        "pid": pid,
        "create_time": create_time,
        "started_at": time.time() if started_at is None else started_at,
    }
    marker.write_text(json.dumps(record), encoding="utf-8", newline="\n")
    return marker


def _live_owner_marker(data_dir: Path, stable_id: str) -> Path:
    """A marker owned by a running process (this one), as another hydrator's would be."""
    me = psutil.Process()
    return _write_owner_marker(
        data_dir, stable_id, pid=me.pid, create_time=me.create_time(), tag="live"
    )


def _stripe_neighbor(data_dir: Path, stable_id: str) -> str:
    """Another track id whose hydrate lock file is the same stripe as ``stable_id``'s."""
    target = stem_hydration.hydrate_lock_path(data_dir, stable_id)
    for n in range(100_000):
        candidate = f"neighbor-{n}"
        if stem_hydration.hydrate_lock_path(data_dir, candidate) == target:
            return candidate
    raise AssertionError("no stripe neighbor found")


@pytest.mark.requirement("STEM-46")
def test_a_hydrate_of_another_track_on_the_same_stripe_is_not_this_tracks_fetch(tmp_path: Path):
    """[if] another track's hydrate holds the shared lock stripe [then] not fetching, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    neighbor = _stripe_neighbor(data_dir, SID)
    assert neighbor != SID
    # The neighbor's hydrate, in another process: the shared stripe lock and
    # the neighbor's live owner marker, none for this track.
    with (
        _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client,
        exclusive_file_lock(stem_hydration.hydrate_lock_path(data_dir, neighbor)),
    ):
        _live_owner_marker(data_dir, neighbor)
        started = time.monotonic()
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        waited = time.monotonic() - started

    assert body["state"] == "error", body
    assert body["error_code"] == "STEM_ARTIFACT_INVALID", body
    assert waited < stems_module.STEM_STATE_SETTLE_S, f"state read waited {waited:.1f}s"


@pytest.mark.requirement("STEM-46")
def test_a_hydrate_of_this_track_queued_behind_a_neighbor_reads_as_fetching(tmp_path: Path):
    """[if] this track's hydrate waits on a neighbor's lock stripe [then] fetching, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    neighbor = _stripe_neighbor(data_dir, SID)
    outcomes: list[stem_hydration.HydrationOutcome] = []
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        with exclusive_file_lock(stem_hydration.hydrate_lock_path(data_dir, neighbor)):
            # A real hydrate of this track: it publishes its owner marker, then
            # blocks on the stripe the neighbor holds, before any temp directory.
            queued = threading.Thread(
                target=lambda: outcomes.append(
                    stem_hydration.hydrate_one(
                        SID,
                        data_dir=data_dir,
                        source=DirectR2Source(cfg=cfg, s3=s3),
                        index=load_cached_index(data_dir),
                        stems_dir=stems_dir,
                    )
                ),
                daemon=True,
            )
            queued.start()
            owners = stem_hydration.hydrate_owners_dir(data_dir)
            deadline = time.monotonic() + 20
            while not list(owners.glob(f"{SID}@*.json")):
                assert time.monotonic() < deadline, "control: the hydrate never published its marker"
                time.sleep(0.01)
            assert not list(stems_dir.glob(f"{SID}{stem_hydration.IN_FLIGHT_MARKER}*")), (
                "control: the hydrate got past the stripe lock"
            )
            during = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        queued.join(timeout=20)
        after = client.get(f"/api/v1/tracks/{SID}/stems/state").json()

    assert during["state"] == "fetching", during
    assert [o.status for o in outcomes] == ["hydrated"], outcomes
    assert after["state"] == "local", after
    assert not list(stem_hydration.hydrate_owners_dir(data_dir).glob(f"{SID}@*")), "marker left behind"


@pytest.mark.requirement("STEM-46")
def test_a_cloud_only_track_another_process_is_hydrating_reads_as_fetching(tmp_path: Path):
    """[if] no local bundle, no error, and another process hydrates it [then] fetching, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        before = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        # Bulk hydrate or a hydrate_runner job: its live owner marker only.
        owner = _live_owner_marker(data_dir, SID)
        during = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        owner.unlink()
        after = client.get(f"/api/v1/tracks/{SID}/stems/state").json()

    assert before["state"] == "cloud", f"control: {before}"
    assert during["state"] == "fetching", during
    assert after["state"] == "cloud", after
    assert SID not in stems_module._INFLIGHT, "control: this route never fetched"


@pytest.mark.requirement("STEM-46")
def test_a_marker_or_temp_dir_left_by_a_dead_hydrate_reads_as_the_error(tmp_path: Path):
    """[if] a killed hydrate left its marker and temp dir [then] the named error, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    exited.wait(timeout=20)
    gone = _write_owner_marker(data_dir, SID, pid=exited.pid, create_time=0.0, tag="exited")
    reused = _write_owner_marker(
        data_dir, SID, pid=os.getpid(), create_time=psutil.Process().create_time() - 60, tag="reused"
    )
    tempfile.mkdtemp(dir=stems_dir, prefix=f"{SID}{stem_hydration.IN_FLIGHT_MARKER}")
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()

    assert body["state"] == "error", body
    assert body["error_code"] == "STEM_ARTIFACT_INVALID", body
    assert not gone.exists(), "a dead owner's marker was kept"
    assert not reused.exists(), "a reused pid's marker was kept"


@pytest.mark.requirement("STEM-46")
def test_a_state_read_during_a_publish_on_its_stripe_reads_the_published_bundle(tmp_path: Path):
    """[if] a publish on the stripe is mid-replacement during the read [then] local, [else stop].

    The holder is in its publish window (generation odd) under the stripe lock
    with no owner marker for this track, as a stripe neighbor's publish would
    be; the read must not answer from the half-replaced bundle.
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    publishing = threading.Event()

    def _publish() -> None:
        with (
            exclusive_file_lock(stem_hydration.hydrate_lock_path(data_dir, SID)),
            stem_hydration.bundle_publish_window(data_dir, SID),
        ):
            publishing.set()
            time.sleep(stems_module.STEM_STATE_SETTLE_S / 10)
            (stems_dir / SID / "vocals.wav").write_bytes(_wav_bytes())

    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        publisher = threading.Thread(target=_publish, daemon=True)
        publisher.start()
        assert publishing.wait(timeout=20), "control: the publish never started"
        assert stem_hydration.read_hydrate_generation(data_dir, SID) % 2 == 1, "control: not odd"
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        publisher.join(timeout=20)

    assert body["state"] == "local", body
    assert stem_hydration.read_hydrate_generation(data_dir, SID) % 2 == 0


@pytest.mark.requirement("STEM-46")
def test_a_publish_landing_during_the_settled_read_is_read_again(tmp_path: Path):
    """[if] this track's repair lands while the settled read is failing [then] local, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    # The 1st failed read is the unchecked first read; the 2nd is the read the
    # generation check brackets.
    pause = _HoldFailedBundleRead(SID, nth=2)
    artifacts_log = logging.getLogger(load_stem_bundle.__module__)
    artifacts_log.addHandler(pause)
    held: dict[str, dict] = {}
    try:
        with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:

            def _read_state() -> None:
                held["body"] = client.get(f"/api/v1/tracks/{SID}/stems/state").json()

            reader = threading.Thread(target=_read_state, daemon=True)
            reader.start()
            assert pause.reached.wait(timeout=20), "state read never reached its settled read"
            _publish_repair("hydrate_one", client, data_dir, stems_dir, cfg, s3)
            pause.resume.set()
            reader.join(timeout=20)
            assert not reader.is_alive(), "held state read never finished"
    finally:
        artifacts_log.removeHandler(pause)

    assert held["body"]["state"] == "local", held["body"]


@pytest.mark.parametrize("unsafe", ["..", "a/b", "/abs/x"])
def test_an_unsafe_stable_id_is_rejected_before_any_path_is_built(tmp_path: Path, unsafe: str):
    """[if] a stable id has a separator or is absolute [then] named error, no path, [else stop]."""
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    outcome = stem_hydration.hydrate_one(
        unsafe,
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=s3),
        index={unsafe: {"manifest.json": "0" * 64}},
        stems_dir=tmp_path / "stems",
    )
    assert outcome.status == "error", outcome
    assert "stable_id" in (outcome.reason or ""), outcome
    assert not data_dir.exists(), sorted(str(p) for p in tmp_path.rglob("*"))
    with pytest.raises(StemArtifactError):
        stem_hydration.hydrate_owner_alive(data_dir, unsafe)


@pytest.mark.requirement("STEM-46")
def test_a_state_read_for_an_unsafe_id_is_the_named_error_not_a_crash(tmp_path: Path):
    """[if] the state route gets an unsafe stable id [then] 200 with the named error, [else stop]."""
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    with _client(tmp_path / "stems", data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.get("/api/v1/tracks/.hidden/stems/state")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "error", resp.json()
    assert resp.json()["error_code"] == "STEM_ARTIFACT_INVALID", resp.json()
    assert not stem_hydration.hydrate_owners_dir(data_dir).exists()


@pytest.mark.requirement("STEM-46")
def test_a_stripe_that_never_settles_on_another_tracks_publish_is_not_fetching(tmp_path: Path):
    """[if] a neighbor keeps the stripe unsettled past the limit [then] error, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    neighbor = _stripe_neighbor(data_dir, SID)
    # The neighbor mid-publish for the whole read: stripe lock held, generation
    # odd, no owner marker for this track.
    with (
        _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client,
        exclusive_file_lock(stem_hydration.hydrate_lock_path(data_dir, neighbor)),
        stem_hydration.bundle_publish_window(data_dir, neighbor),
    ):
        assert stem_hydration.read_hydrate_generation(data_dir, SID) % 2 == 1, "control: not odd"
        started = time.monotonic()
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        waited = time.monotonic() - started

    assert waited >= stems_module.STEM_STATE_SETTLE_S, f"control: deadline not reached ({waited:.2f}s)"
    assert body["state"] == "error", body
    assert body["error_code"] == "STEM_ARTIFACT_INVALID", body


@pytest.mark.requirement("STEM-46")
def test_an_owner_appearing_during_the_settled_read_reads_as_fetching(tmp_path: Path):
    """[if] a hydrate of this track starts during the settled read [then] fetching, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    pause = _HoldFailedBundleRead(SID, nth=2)
    artifacts_log = logging.getLogger(load_stem_bundle.__module__)
    artifacts_log.addHandler(pause)
    held: dict[str, dict] = {}
    try:
        with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:

            def _read_state() -> None:
                held["body"] = client.get(f"/api/v1/tracks/{SID}/stems/state").json()

            reader = threading.Thread(target=_read_state, daemon=True)
            reader.start()
            assert pause.reached.wait(timeout=20), "state read never reached its settled read"
            generation = stem_hydration.read_hydrate_generation(data_dir, SID)
            # A bulk or runner hydrate starts: marker first, still downloading.
            _live_owner_marker(data_dir, SID)
            pause.resume.set()
            reader.join(timeout=20)
            assert not reader.is_alive(), "held state read never finished"
            assert stem_hydration.read_hydrate_generation(data_dir, SID) == generation, (
                "control: the generation moved, so the recheck was not what answered"
            )
    finally:
        artifacts_log.removeHandler(pause)

    assert held["body"]["state"] == "fetching", held["body"]


@pytest.mark.requirement("STEM-45")
def test_a_fetch_by_another_process_reports_the_indexed_file_total(tmp_path: Path):
    """[if] another process fetches the track [then] progress names the indexed total, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    entry = _seed_bundle(s3, cfg, SID)
    save_cached_index(data_dir, {SID: entry})
    stems_dir.mkdir(parents=True)
    marker = _live_owner_marker(data_dir, SID)
    attempt = marker.name.removeprefix(f"{SID}@").removesuffix(".json")
    fetch_dir = Path(
        tempfile.mkdtemp(
            dir=stems_dir,
            prefix=f"{SID}{stem_hydration.IN_FLIGHT_MARKER}{attempt}-",
        )
    )
    (fetch_dir / "manifest.json").write_bytes(b"x" * 10)
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()

    try:
        assert SID not in stems_module._INFLIGHT, "control: this route never fetched"
        assert body["state"] == "fetching", body
        assert body["progress"] == {"files_total": len(entry), "files_done": 1, "bytes_done": 10}, body
    finally:
        marker.unlink(missing_ok=True)


def _fail_with_sharing_violation(
    real: Any, is_target: Any, *, times: int | None
) -> Any:
    """Wrap os.replace or Path.unlink: raise PermissionError for matching paths
    ``times`` times (``None``: always), as Windows does for a file another
    process holds open. Fault injection only."""
    left = {"n": times}

    def _flaky(*args: Any, **kwargs: Any) -> Any:
        if is_target(*args) and (left["n"] is None or left["n"] > 0):
            if left["n"] is not None:
                left["n"] -= 1
            raise PermissionError(13, "sharing violation", str(args[-1]))
        return real(*args, **kwargs)

    return _flaky


def _owner_or_generation(*args: Any) -> bool:
    target = Path(args[-1])
    return target.suffix == ".gen" or target.parent.name == "stem-hydrate-owners"


@pytest.mark.requirement("STEM-46")
def test_a_publish_survives_one_sharing_violation_on_each_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] each marker or generation replace is refused once [then] it still lands, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    refused: list[str] = []
    real_replace = os.replace

    def _record(*args: Any) -> bool:
        hit = _owner_or_generation(*args) and Path(args[-1]).name not in refused
        if hit:
            refused.append(Path(args[-1]).name)
        return hit

    monkeypatch.setattr(os, "replace", _fail_with_sharing_violation(real_replace, _record, times=None))
    outcome = stem_hydration.hydrate_one(
        SID,
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=s3),
        index=load_cached_index(data_dir),
        stems_dir=stems_dir,
    )
    assert outcome.status == "hydrated", outcome
    assert any(name.endswith(".gen") for name in refused), f"control: {refused}"
    assert any(name.endswith(".json") for name in refused), f"control: {refused}"
    assert stem_hydration.read_hydrate_generation(data_dir, SID) % 2 == 0
    assert not list(stem_hydration.hydrate_owners_dir(data_dir).glob(f"{SID}@*")), "marker left"


@pytest.mark.requirement("STEM-46")
def test_a_marker_unlink_refused_once_keeps_the_recorded_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] a marker removal is refused once [then] the fetch error answers, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    good = _seed_bundle(s3, cfg, SID)
    save_cached_index(data_dir, {SID: {**good, "vocals.wav": "0" * 64}})
    monkeypatch.setattr(
        Path,
        "unlink",
        _fail_with_sharing_violation(Path.unlink, _owner_or_generation, times=1),
    )
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        client.get(f"/api/v1/tracks/{SID}/stems")
        stems_module._INFLIGHT[SID].result(timeout=20)
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()

    assert body["state"] == "error", body
    assert body["error_code"] == "STEM_BUNDLE_HYDRATION_FAILED", body
    assert not list(stem_hydration.hydrate_owners_dir(data_dir).glob(f"{SID}@*")), "marker left"


@pytest.mark.requirement("STEM-46")
def test_a_marker_that_cannot_be_removed_does_not_read_as_fetching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] a marker can never be removed [then] the outcome stands, not fetching, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    good = _seed_bundle(s3, cfg, SID)
    save_cached_index(data_dir, {SID: {**good, "vocals.wav": "0" * 64}})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    try:
        with monkeypatch.context() as patched:
            patched.setattr(
                Path,
                "unlink",
                _fail_with_sharing_violation(Path.unlink, _owner_or_generation, times=None),
            )
            outcome = stem_hydration.hydrate_one(
                SID,
                data_dir=data_dir,
                source=DirectR2Source(cfg=cfg, s3=s3),
                index=load_cached_index(data_dir),
                stems_dir=stems_dir,
            )
            markers = list(stem_hydration.hydrate_owners_dir(data_dir).glob(f"{SID}@*.json"))
            assert markers, "control: the marker was removed after all"
            assert all(m.with_name(f"{m.name}.done").exists() for m in markers), "no .done sibling"
            # A reader in another process has none of this process's memory.
            stem_hydration._ABANDONED_MARKERS.clear()
            with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
                body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    finally:
        stem_hydration._ABANDONED_MARKERS.clear()

    assert outcome.status == "error", outcome
    assert body["state"] == "error", body
    assert body["error_code"] == "STEM_ARTIFACT_INVALID", body


@pytest.mark.requirement("STEM-46")
def test_a_generation_that_cannot_be_written_is_the_named_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] the stripe generation can never be written [then] hydrate names it, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    monkeypatch.setattr(
        os,
        "replace",
        _fail_with_sharing_violation(os.replace, lambda *a: Path(a[-1]).suffix == ".gen", times=None),
    )
    outcome = stem_hydration.hydrate_one(
        SID,
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=s3),
        index=load_cached_index(data_dir),
        stems_dir=stems_dir,
    )
    assert outcome.status == "error", outcome
    assert "could not write" in (outcome.reason or ""), outcome
    assert (stems_dir / SID / "vocals.wav").read_bytes() == b"not a wav", "old bundle was touched"


@pytest.mark.requirement("STEM-46")
def test_a_refused_closing_generation_write_keeps_the_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] the closing generation write is always refused [then] the publish stands, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    gen_writes = {"n": 0}

    def _closing_gen_write(*args: Any) -> bool:
        if Path(args[-1]).suffix != ".gen":
            return False
        gen_writes["n"] += 1
        return gen_writes["n"] > 1  # the opening write lands; every later one is refused

    monkeypatch.setattr(
        os, "replace", _fail_with_sharing_violation(os.replace, _closing_gen_write, times=None)
    )
    outcome = stem_hydration.hydrate_one(
        SID,
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=s3),
        index=load_cached_index(data_dir),
        stems_dir=stems_dir,
    )
    monkeypatch.undo()

    assert outcome.status == "hydrated", outcome
    assert gen_writes["n"] > 1, "control: the closing write was never attempted"
    assert stem_hydration.read_hydrate_generation(data_dir, SID) % 2 == 1, "control: closed anyway"
    assert stem_bundles.hydrate_pins(stems_dir / SID) == [], "hydrate pin left behind"
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    assert body["state"] == "local", body


@pytest.mark.requirement("STEM-46")
def test_a_marker_that_cannot_be_removed_or_marked_done_stays_ignored_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] the marker can be neither removed nor marked done [then] not fetching here, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    good = _seed_bundle(s3, cfg, SID)
    save_cached_index(data_dir, {SID: {**good, "vocals.wav": "0" * 64}})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    try:
        with monkeypatch.context() as patched:
            patched.setattr(
                Path, "unlink", _fail_with_sharing_violation(Path.unlink, _owner_or_generation, times=None)
            )
            patched.setattr(
                stem_hydration,
                "_create_done_sibling",
                _fail_with_sharing_violation(
                    stem_hydration._create_done_sibling,
                    lambda *a: Path(a[0]).name.endswith(".done"),
                    times=None,
                ),
            )
            stem_hydration.hydrate_one(
                SID,
                data_dir=data_dir,
                source=DirectR2Source(cfg=cfg, s3=s3),
                index=load_cached_index(data_dir),
                stems_dir=stems_dir,
            )
            assert stem_hydration._ABANDONED_MARKERS, "control: the fallback set was not used"
            with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
                body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    finally:
        stem_hydration._ABANDONED_MARKERS.clear()

    assert body["state"] == "error", body


@pytest.mark.requirement("STEM-46")
def test_a_state_read_while_a_hydrate_inspects_the_local_bundle_reads_as_fetching(tmp_path: Path):
    """[if] a hydrate is still inspecting the local bundle [then] fetching, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    # The 1st failed read is the hydrate's own look at the local bundle.
    pause = _HoldFailedBundleRead(SID)
    artifacts_log = logging.getLogger(load_stem_bundle.__module__)
    artifacts_log.addHandler(pause)
    outcomes: list[stem_hydration.HydrationOutcome] = []
    try:
        with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
            hydrator = threading.Thread(
                target=lambda: outcomes.append(
                    stem_hydration.hydrate_one(
                        SID,
                        data_dir=data_dir,
                        source=DirectR2Source(cfg=cfg, s3=s3),
                        index=load_cached_index(data_dir),
                        stems_dir=stems_dir,
                    )
                ),
                daemon=True,
            )
            hydrator.start()
            assert pause.reached.wait(timeout=20), "control: the hydrate never inspected the bundle"
            during = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
            pause.resume.set()
            hydrator.join(timeout=20)
            after = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    finally:
        artifacts_log.removeHandler(pause)

    assert during["state"] == "fetching", during
    assert [o.status for o in outcomes] == ["hydrated"], outcomes
    assert after["state"] == "local", after


def _is_generation(*args: Any) -> bool:
    return Path(args[-1]).suffix == ".gen"


def _is_owner_marker(*args: Any) -> bool:
    return Path(args[-1]).parent.name == "stem-hydrate-owners"


@pytest.mark.requirement("STEM-46")
def test_a_generation_read_refused_once_still_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] a generation read is refused once [then] the publish still lands, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    monkeypatch.setattr(
        Path, "read_text", _fail_with_sharing_violation(Path.read_text, _is_generation, times=1)
    )
    outcome = stem_hydration.hydrate_one(
        SID,
        data_dir=data_dir,
        source=DirectR2Source(cfg=cfg, s3=s3),
        index=load_cached_index(data_dir),
        stems_dir=stems_dir,
    )
    assert outcome.status == "hydrated", outcome


@pytest.mark.requirement("STEM-46")
def test_an_owner_marker_read_refused_once_still_reads_as_fetching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] an owner marker read is refused once [then] its live owner reads fetching, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    _live_owner_marker(data_dir, SID)
    monkeypatch.setattr(
        Path, "read_text", _fail_with_sharing_violation(Path.read_text, _is_owner_marker, times=1)
    )
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    assert body["state"] == "fetching", body


@pytest.mark.requirement("STEM-46")
def test_a_generation_that_cannot_be_read_is_the_named_error_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] the stripe generation can never be read [then] a named error, not 500, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    monkeypatch.setattr(
        Path, "read_text", _fail_with_sharing_violation(Path.read_text, _is_generation, times=None)
    )
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.get(f"/api/v1/tracks/{SID}/stems/state")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "error", resp.json()
    assert resp.json()["error_code"] == "STEM_ARTIFACT_INVALID", resp.json()


@pytest.mark.requirement("STEM-46")
def test_an_owner_whose_pid_cannot_be_inspected_does_not_read_as_fetching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] an owner pid's start time cannot be read [then] not fetching, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    marker = _live_owner_marker(data_dir, SID)

    def _denied(self: psutil.Process) -> float:
        raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil.Process, "create_time", _denied)
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    assert marker.exists(), "an uninspectable owner's marker was removed"
    assert body["state"] == "error", body
    assert body["error_code"] == "STEM_ARTIFACT_INVALID", body


@pytest.mark.requirement("STEM-46")
def test_an_owner_appearing_before_the_settle_deadline_reads_as_fetching(tmp_path: Path):
    """[if] this track's hydrate starts during the last unsettled read [then] fetching, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    neighbor = _stripe_neighbor(data_dir, SID)
    # The 2nd failed read is the first generation-checked read. While it is
    # held, the deadline passes, a neighbor's publish moves the generation
    # (so the read is unsettled), and this track's hydrate publishes its marker.
    pause = _HoldFailedBundleRead(SID, nth=2)
    artifacts_log = logging.getLogger(load_stem_bundle.__module__)
    artifacts_log.addHandler(pause)
    held: dict[str, dict] = {}
    try:
        with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:

            def _read_state() -> None:
                held["body"] = client.get(f"/api/v1/tracks/{SID}/stems/state").json()

            reader = threading.Thread(target=_read_state, daemon=True)
            reader.start()
            assert pause.reached.wait(timeout=20), "state read never reached its settled read"
            time.sleep(stems_module.STEM_STATE_SETTLE_S + 0.2)
            before = stem_hydration.read_hydrate_generation(data_dir, SID)
            with (
                exclusive_file_lock(stem_hydration.hydrate_lock_path(data_dir, neighbor)),
                stem_hydration.bundle_publish_window(data_dir, neighbor),
            ):
                pass
            assert stem_hydration.read_hydrate_generation(data_dir, SID) != before, "control"
            _live_owner_marker(data_dir, SID)
            pause.resume.set()
            reader.join(timeout=20)
            assert not reader.is_alive(), "held state read never finished"
    finally:
        artifacts_log.removeHandler(pause)

    assert held["body"]["state"] == "fetching", held["body"]


@pytest.mark.requirement("STEM-12")
def test_an_owner_marker_with_nan_create_time_is_removed_not_fetching(tmp_path: Path):
    """[if] marker has NaN create_time naming this pid [then] error not fetching, marker gone, [else stop].

    MUTATION TARGET: allow non-finite stamps in shape and the track reads fetching forever.
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    me = psutil.Process()
    owners = stem_hydration.hydrate_owners_dir(data_dir)
    owners.mkdir(parents=True)
    nan_marker = owners / f"{SID}@{me.pid}-nanlive.json"
    nan_marker.write_text(
        json.dumps({"pid": me.pid, "create_time": float("nan"), "started_at": time.time()}, allow_nan=True),
        encoding="utf-8",
        newline="\n",
    )
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        fresh = _live_owner_marker(data_dir, SID)
        control = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        fresh.unlink()

    assert control["state"] == "fetching", f"control: {control}"
    assert body["state"] == "error", body
    assert body["error_code"] == "STEM_ARTIFACT_INVALID", body
    assert not nan_marker.exists(), "non-finite marker was kept"


@pytest.mark.requirement("STEM-46")
def test_an_owner_marker_older_than_the_hydrate_ttl_is_not_a_fetch(tmp_path: Path):
    """[if] a live owner's marker is older than the hydrate TTL [then] not fetching, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _write_local_bundle(stems_dir, SID)
    (stems_dir / SID / "vocals.wav").write_bytes(b"not a wav")
    me = psutil.Process()
    stale = _write_owner_marker(
        data_dir,
        SID,
        pid=me.pid,
        create_time=me.create_time(),
        tag="stale",
        started_at=time.time() - stem_hydration.HYDRATE_OWNER_TTL_S - 60,
    )
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        fresh = _live_owner_marker(data_dir, SID)
        control = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
        fresh.unlink()

    assert control["state"] == "fetching", f"control: a fresh live marker must count: {control}"
    assert body["state"] == "error", body
    assert body["error_code"] == "STEM_ARTIFACT_INVALID", body
    assert stale.exists(), "an expired marker of a live owner was removed"


def test_the_hydrate_locks_are_taken_only_when_free(tmp_path: Path):
    """[if] a hydrate lock is held [then] not taken; free [then] taken, released, [else stop]."""
    with stem_hydration.hydrate_locks_within(tmp_path, SID, 0) as taken:
        assert taken is True
        with stem_hydration.hydrate_locks_within(tmp_path, SID, 0) as again:
            assert again is False, "in-process lock was not held"
    with (
        exclusive_file_lock(stem_hydration.hydrate_lock_path(tmp_path, SID)),
        stem_hydration.hydrate_locks_within(tmp_path, SID, 0.05) as taken,
    ):
        assert taken is False, "cross-process file lock was not respected"
    with stem_hydration.hydrate_locks_within(tmp_path, SID, 0) as taken:
        assert taken is True, "a lock was not released"


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
    """[if] arming failed and the cached index lacks it [then] none; no cache, error, [else stop]."""
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


@pytest.mark.requirement("STEM-45")
def test_hydration_progress_counts_only_directories_for_live_attempt_suffixes(tmp_path: Path):
    """[if] stale and offload dirs share the prefix [then] only live attempt counts, [else stop].

    MUTATION TARGET: prefix-only matching inflates files_done past the real attempt.
    """
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir(parents=True)
    data_dir = tmp_path / "data"
    sfx = "9-abcdef01"
    live = Path(
        tempfile.mkdtemp(
            dir=stems_dir,
            prefix=f"{SID}{stem_hydration.IN_FLIGHT_MARKER}{sfx}-",
        )
    )
    (live / "a.bin").write_bytes(b"x")
    stale = Path(tempfile.mkdtemp(dir=stems_dir, prefix=f"{SID}{stem_hydration.IN_FLIGHT_MARKER}stale-"))
    (stale / "b.bin").write_bytes(b"yy")
    (stale / "c.bin").write_bytes(b"zz")
    offload = stems_dir / f"{SID}{stem_hydration.IN_FLIGHT_MARKER}deadbeef"
    offload.mkdir()
    (offload / "d.bin").write_bytes(b"www")

    suffixes = frozenset({sfx})
    p = stems_module._hydration_progress(SID, stems_dir, data_dir=Path(data_dir), attempt_suffixes=suffixes)
    assert p.files_done == 1
    assert p.bytes_done == 1


@pytest.mark.requirement("STEM-46")
def test_a_finished_route_fetch_total_does_not_size_a_later_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] a route fetch finished [then] later progress does not read its total, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    done: Future[None] = Future()
    done.set_result(None)
    monkeypatch.setitem(stems_module._INFLIGHT, SID, done)
    monkeypatch.setitem(stems_module._INFLIGHT_FILE_TOTALS, SID, 5)
    progress = stems_module._hydration_progress(SID, stems_dir, data_dir=data_dir)
    assert progress.files_total == 0
    running: Future[None] = Future()
    monkeypatch.setitem(stems_module._INFLIGHT, SID, running)
    assert stems_module._hydration_progress(SID, stems_dir, data_dir=data_dir).files_total == 5
    running.set_result(None)


@pytest.mark.requirement("STEM-46")
def test_an_owner_published_after_the_local_read_reads_as_fetching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] an owner is published after the local read [then] it reads as fetching, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    real_local_read = stems_module._local_bundle_read

    def _owner_appears_after_the_check(stable_id, stems_dir, request):
        real_local_read(stable_id, stems_dir, request)
        _live_owner_marker(data_dir, stable_id)

    monkeypatch.setattr(stems_module, "_local_bundle_read", _owner_appears_after_the_check)
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    assert body["state"] == "fetching", body


@pytest.mark.requirement("STEM-46")
def test_a_fetch_the_local_read_saw_that_has_finished_is_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] a fetch seen by the local read has finished [then] the bundle is read again, [else stop].

    MUTATION TARGET: drop the re-read and the first observation reads as fetching.
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    reads: list[str] = []

    def _fetch_seen_then_repaired(stable_id, stems_dir, request):
        reads.append(stable_id)
        return "fetching" if len(reads) == 1 else "local"

    monkeypatch.setattr(stems_module, "_local_bundle_read", _fetch_seen_then_repaired)
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    assert body["state"] == "local", body
    assert len(reads) == 2


@pytest.mark.requirement("STEM-46")
def test_a_live_owner_reads_as_fetching_beside_an_artifact_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] an owner is live beside an artifact error [then] the state is fetching, [else stop].

    MUTATION TARGET: let the first read's error win over a live owner and this test fails.
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})

    def _error_while_an_owner_publishes(stable_id, stems_dir, request):
        _live_owner_marker(data_dir, stable_id)
        return "stem bundle on disk is unreadable"

    monkeypatch.setattr(stems_module, "_local_bundle_read", _error_while_an_owner_publishes)
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    assert body["state"] == "fetching", body


@pytest.mark.requirement("STEM-46")
def test_an_artifact_error_a_finished_hydrate_repaired_is_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] a repair landed after an artifact error [then] the bundle reads as local, [else stop].

    MUTATION TARGET: answer the first read's error without the generation-bracketed read.
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    settled: list[str] = []

    def _settled_as_local(stable_id, stems_dir, request, data_dir):
        settled.append(stable_id)
        return "local"

    unreadable = "stem bundle on disk is unreadable"
    monkeypatch.setattr(stems_module, "_read_bundle", lambda *_args: unreadable)
    monkeypatch.setattr(stems_module, "_read_against_generation", _settled_as_local)
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    assert body["state"] == "local", body
    assert settled, "an artifact error never reached the generation-bracketed read"


@pytest.mark.requirement("STEM-46")
def test_a_clean_miss_is_settled_by_the_generation_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] the first read misses [then] the generation-bracketed read decides, [else stop].

    MUTATION TARGET: return a clean miss without the settled read and the bundle a
    hydrate landed mid-read answers none.
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    settled: list[str] = []

    def _settled_as_local(stable_id, stems_dir, request, data_dir):
        settled.append(stable_id)
        return "local"

    monkeypatch.setattr(stems_module, "_read_bundle", lambda *_args: None)
    monkeypatch.setattr(stems_module, "_read_against_generation", _settled_as_local)
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        body = client.get(f"/api/v1/tracks/{SID}/stems/state").json()
    assert body["state"] == "local", body
    assert settled, "a clean miss never reached the generation-bracketed read"


@pytest.mark.requirement("STEM-46")
def test_a_corrupt_index_during_a_fetch_is_a_named_error_not_missing_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] a fetch runs over a corrupt index [then] the state route answers 502, [else stop]."""
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    save_cached_index(data_dir, {SID: _seed_bundle(s3, cfg, SID)})
    _live_owner_marker(data_dir, SID)
    stems_module.stem_index.local_index_cache_path(data_dir).write_text(
        "not json at all", encoding="utf-8", newline="\n"
    )
    with _client(stems_dir, data_dir=data_dir, hydration_cfg=cfg, hydration_s3=s3) as client:
        resp = client.get(f"/api/v1/tracks/{SID}/stems/state")
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["code"] == "STEM_INDEX_CORRUPT"


@pytest.mark.requirement("STEM-46")
def test_a_new_owner_is_not_read_as_finished_by_a_stale_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """[if] a stale tombstone sits by a new owner [then] the owner reads as fetching, [else stop]."""
    data_dir = tmp_path / "data"
    monkeypatch.setattr(stem_hydration.secrets, "token_hex", lambda _n: "deadbeef")
    owners = stem_hydration.hydrate_owners_dir(data_dir)
    owners.mkdir(parents=True)
    me = psutil.Process()
    stale = owners / f"{SID}@{me.pid}-deadbeef.json.done"
    stale.touch()
    with stem_hydration._hydrate_owner_marked(data_dir, SID):
        assert stem_hydration.hydrate_owner_alive(data_dir, SID), "a stale tombstone hid a live owner"


def _closed_loopback_url() -> str:
    """An http URL on a loopback port nothing listens on, so any fetch fails fast."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.mark.requirement("STEM-44")
def test_a_missing_part_of_a_local_bundle_is_a_404_and_never_hydrates(tmp_path: Path):
    """[if] a local bundle lacks the asked part [then] 404 STEM_PART_NOT_FOUND, no fetch, [else stop].

    With hydration armed and the bundle absent from the index, treating the
    part-level 404 as a missing bundle would start a hydration and answer
    502 STEM_BUNDLE_NOT_INDEXED instead (Codex on #6821). Hydration is armed
    with the production hub source and its real HTTP transport, aimed at a
    loopback port nothing serves, so no fake provider stands in for it.
    Control: the same request for a part the bundle has is served.
    """
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    save_cached_index(data_dir, {})
    _write_local_bundle(stems_dir, SID)
    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.state.stem_hydration_data_dir = data_dir
    app.state.stem_hydration_source = HubPresignedSource(
        data_dir=data_dir,
        hub_url=_closed_loopback_url(),
        machine_id="machine-1",
        bearer=None,
    )
    app.state.stem_hydration_cfg = None
    app.state.stem_hydration_s3 = None
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:
        missing = client.get(f"/api/v1/tracks/{SID}/stems/not-a-part")
        present = client.get(f"/api/v1/tracks/{SID}/stems/vocals")
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"]["code"] == "STEM_PART_NOT_FOUND"
    assert SID not in stems_module._INFLIGHT
    assert present.status_code == 200, present.text
