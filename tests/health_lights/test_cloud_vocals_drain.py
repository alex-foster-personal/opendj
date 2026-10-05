"""Vocals for bundles evicted to R2: fetch one, derive, never fight the evictor (HEALTH-08).

Regression lines:
  - if the drain fetches more than one bundle per job then broken
  - if more than transient_bundle_cap fetched bundles are ever on disk then broken
  - if the drain fetches while the stem cache has no room above its floor then broken
  - if a bundle is fetched for a track whose vocals already exist then broken
  - if the same bundle is downloaded twice while the evictor keeps removing it then broken
  - if a hub outage spends one of a track's three attempts then broken
  - if the drain removes a bundle a deck has open, or one R2 does not confirm, then broken

[if] the drain fetches more than one cloud bundle per job or fights the evictor [then] fail, [else stop].
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apps.cloud import stem_cache_budget
from apps.cloud.stem_cache_budget import GIB, DiskUsage
from apps.cloud.stem_source import STEM_HUB_UNREACHABLE, DirectR2Source, StemSourceError
from apps.lyrics.service import LyricsFetchService
from apps.vocals import cache as vocals_cache
from apps.webui.server import coverage_cloud_vocals as ccv
from apps.webui.server import coverage_drain as cd
from apps.webui.server import coverage_outcomes as co
from apps.webui.server.routes import ingest as ingest_mod
from tests.cloudsync.conftest import InMemoryAssetS3
from tests.health_lights import fixtures as fx
from tests.health_lights.conftest import Library
from tests.health_lights.test_coverage_drain import Clock, Lrclib

pytestmark = pytest.mark.requirement("HEALTH-08")

VOLUME: int = 460 * GIB
ROOMY: int = 200 * GIB


@pytest.fixture
def cfg():
    from apps.cloud.config import CloudConfig

    return CloudConfig(
        r2_account_id="acct1234", r2_access_key_id="AKIDEXAMPLE",
        r2_secret_access_key="secret-key", state_bucket="test-state",
        audio_bucket="test-audio", hostname="test-host", bind_host="127.0.0.1",
    )


class CountingSource(DirectR2Source):
    """The real direct-R2 source, recording which bundle each fetch was for."""

    def __init__(self, cfg, s3) -> None:
        super().__init__(cfg, s3)
        self.fetched: list[str] = []
        self.unreachable = False

    def fetch_bundle_files(self, *, stable_id, file_hashes, tmp_dir):
        if self.unreachable:
            raise StemSourceError(STEM_HUB_UNREACHABLE, "hub unreachable: connection refused")
        self.fetched.append(stable_id)
        return super().fetch_bundle_files(
            stable_id=stable_id, file_hashes=file_hashes, tmp_dir=tmp_dir
        )


class CloudRig:
    """A real drain + real CloudVocals over the in-memory R2 and a tmp library."""

    def __init__(
        self, library: Library, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        *, cloud_ids: tuple[str, ...], cap: int = 2, lrclib: Lrclib | None = None,
    ) -> None:
        self.library = library
        self.s3 = InMemoryAssetS3()
        self.free = ROOMY
        self.protected: set[str] = set()
        self.clock = Clock()
        self.vocals_runs: list[str] = []
        self.lyrics_runs: list[str] = []
        #: Bundle directories on disk at the moment each vocals job derived.
        self.on_disk_at_derive: list[int] = []
        self.audio: dict[str, Path] = {}
        self.index: dict[str, dict[str, str]] = {}
        for stable_id in cloud_ids:
            self.audio[stable_id] = fx.audio_file(library.music, f"{stable_id}.mp3")
            fx.seed_track(library.state_db, stable_id, str(self.audio[stable_id]))
            self.index[stable_id] = fx.put_in_cloud(
                self.s3, cfg, tmp_path / "scratch", stable_id
            )
            if lrclib is None:
                # Lyrics are not under test: already there, so green is reachable.
                fx.write_lyrics(library.lyrics_cache, stable_id)
        self.source = CountingSource(cfg, self.s3)
        fx.arm_cloud(library.app, library.data_dir, self.s3, cfg, self.index)
        library.app.state.stem_hydration_source = self.source
        # hydrate_one's own post-fetch enforce measures the volume too.
        monkeypatch.setattr(stem_cache_budget, "measure_disk", self._disk)
        self.outcomes = co.OutcomeStore(co.store_path(library.data_dir))
        self.config = cd.DrainConfig(cd.config_path(library.data_dir))
        self.config.update(transient_bundle_cap=cap)
        self.cloud = ccv.CloudVocals(
            data_dir=library.data_dir,
            inputs_fn=self._inputs,
            cap_fn=self.config.transient_bundle_cap,
            derive=self._derive,
            disk_fn=self._disk,
            clock=self.clock,
        )
        service = LyricsFetchService(library.data_dir, provider=lrclib or Lrclib({}))
        self.drain = cd.CoverageDrain(
            snapshot_fn=lambda: ingest_mod.build_snapshot(library.app),
            jobs={"vocals": self._derive, "lyrics": cd.lyrics_job(service, self.lyrics_runs)},
            playing_fn=lambda: False,
            outcomes=self.outcomes,
            config=self.config,
            clock=self.clock,
            cloud_vocals=self.cloud,
        )

    def _disk(self, _path: Path) -> DiskUsage:
        return DiskUsage(total_bytes=VOLUME, free_bytes=self.free)

    def _inputs(self) -> ccv.CloudInputs:
        return ccv.CloudInputs(
            stems_dir=self.library.stems, index=self.index,
            protected=frozenset(self.protected), source=self.source,
        )

    def _derive(self, stable_id: str, audio_path: str) -> None:
        # The real job reads the bundle; a derive with no bundle is a bug here.
        assert (self.library.stems / stable_id / "manifest.json").is_file(), stable_id
        self.on_disk_at_derive.append(len(self.bundles()))
        self.vocals_runs.append(stable_id)
        fx.write_vocals(self.library.vocal_cache, stable_id, Path(audio_path))

    def bundles(self) -> list[str]:
        if not self.library.stems.is_dir():
            return []
        return sorted(p.name for p in self.library.stems.iterdir() if p.is_dir())

    def evict_everything(self) -> None:
        """The real evictor at its most aggressive: a full disk, so every
        R2-confirmed bundle goes."""
        report = stem_cache_budget.enforce(
            self.library.stems, data_dir=self.library.data_dir, index=self.index,
            protected=frozenset(), can_rehydrate=True,
            disk=DiskUsage(total_bytes=VOLUME, free_bytes=0),
        )
        assert self.bundles() == [], report


def test_one_bundle_per_job_until_every_cloud_track_has_vocals(
    library: Library, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = CloudRig(library, cfg, tmp_path, monkeypatch, cloud_ids=("a", "b", "c"))

    outcomes = []
    for _ in range(3):
        before = len(rig.source.fetched)
        outcomes.append(rig.drain.tick())
        assert len(rig.source.fetched) - before == 1      # exactly one fetch per job

    assert outcomes == ["ran:vocals"] * 3
    assert sorted(rig.vocals_runs) == ["a", "b", "c"]
    assert sorted(rig.source.fetched) == ["a", "b", "c"]
    status = rig.drain.status()
    assert status.pending["vocals"] == 0
    assert status.cloud_vocals["bundles_downloaded"] == 3
    assert status.cloud_vocals["bytes_downloaded"] > 0


@pytest.mark.parametrize(("cap", "peak"), [(1, 1), (2, 2), (4, 4)])
def test_transient_bundles_never_exceed_the_cap(
    library: Library, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cap: int, peak: int
) -> None:
    """[if] the drain has fetched ``cap`` bundles [then] it releases its
    oldest before the next. The three caps prove the cap is what binds: a
    drain that always kept one would pass cap=1 and fail the peak for 2 and 4.
    """
    ids = ("a", "b", "c", "d", "e")
    rig = CloudRig(library, cfg, tmp_path, monkeypatch, cloud_ids=ids, cap=cap)

    for _ in ids:
        assert rig.drain.tick() == "ran:vocals"
        assert len(rig.bundles()) <= cap

    assert max(rig.on_disk_at_derive) == peak
    assert len(rig.cloud.held()) <= cap
    assert rig.drain.status().cloud_vocals["transient_held"] == min(cap, len(ids))


def test_drain_and_evictor_reach_a_fixed_point_with_one_download_per_bundle(
    library: Library, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The union defect: the evictor removes every bundle between drain jobs.
    Each bundle must still be downloaded exactly once, and once every track
    has vocals the drain asks for nothing, whatever the evictor does."""
    ids = ("a", "b", "c", "d")
    rig = CloudRig(library, cfg, tmp_path, monkeypatch, cloud_ids=ids)

    states = []
    for _ in range(12):
        states.append(rig.drain.tick())
        rig.evict_everything()

    assert sorted(rig.source.fetched) == sorted(ids)       # once each, not once per tick
    assert states[:4] == ["ran:vocals"] * 4
    assert set(states[4:]) == {"green"}
    snapshot = ingest_mod.build_snapshot(library.app)
    assert snapshot.stems_in_cloud == 4 and snapshot.stems_local == 0
    assert snapshot.counts["vocals"].pending == 0
    assert list(snapshot.vocals_cloud_ready) == []


def test_no_fetch_while_the_cache_has_no_room_above_the_floor(
    library: Library, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = CloudRig(library, cfg, tmp_path, monkeypatch, cloud_ids=("a",))
    floor = stem_cache_budget.floor_bytes(VOLUME, stem_cache_budget.StemCacheSettings())
    # Above the floor, but by less than the headroom two transient slots need.
    rig.free = floor + ccv.HEADROOM_BYTES_PER_BUNDLE

    assert rig.drain.tick() == "blocked"
    assert rig.source.fetched == []
    status = rig.drain.status()
    assert "would force an eviction" in status.cloud_vocals["paused_reason"]
    assert "1 vocals wait on a stem download" in str(status.reason)

    # Control: the same track IS fetched once there is room, so the empty
    # list above is the gate and not a rig that could never fetch.
    rig.free = floor + 2 * ccv.HEADROOM_BYTES_PER_BUNDLE
    assert rig.drain.tick() == "ran:vocals"
    assert rig.source.fetched == ["a"]


def test_cap_zero_switches_cloud_vocals_off(
    library: Library, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = CloudRig(library, cfg, tmp_path, monkeypatch, cloud_ids=("a",), cap=0)

    assert rig.drain.tick() == "blocked"
    assert rig.source.fetched == []
    assert "switched off" in rig.drain.status().cloud_vocals["paused_reason"]


def test_a_track_with_vocals_is_never_fetched_for(
    library: Library, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = CloudRig(library, cfg, tmp_path, monkeypatch, cloud_ids=("a", "b"))
    fx.write_vocals(library.vocal_cache, "a", rig.audio["a"])

    snapshot = ingest_mod.build_snapshot(library.app)
    assert [sid for sid, _path in snapshot.vocals_cloud_ready] == ["b"]
    # And the runner itself refuses, even when asked directly.
    rig.cloud.run("a", str(rig.audio["a"]))
    assert rig.source.fetched == [] and rig.vocals_runs == []

    assert rig.drain.tick() == "ran:vocals"
    assert rig.source.fetched == ["b"]


def test_a_hub_outage_pauses_cloud_vocals_and_spends_no_attempt(
    library: Library, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = CloudRig(library, cfg, tmp_path, monkeypatch, cloud_ids=("a",))
    rig.source.unreachable = True

    assert rig.drain.tick() == "blocked"
    assert "cloud vocals paused" in str(rig.drain.status().reason)
    assert rig.outcomes.load() == {}                 # no attempt recorded against the track
    assert rig.drain.status().jobs_failed == 0

    # Healed, but inside the pause: nothing is tried.
    rig.source.unreachable = False
    assert rig.drain.tick() == "blocked"
    assert rig.source.fetched == []
    assert "paused after a hub failure" in rig.drain.status().cloud_vocals["paused_reason"]

    rig.clock.now += ccv.PAUSE_S + 1
    assert rig.drain.tick() == "ran:vocals"
    assert rig.source.fetched == ["a"]


def test_a_bundle_open_on_a_deck_is_never_removed_by_the_drain(
    library: Library, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = CloudRig(library, cfg, tmp_path, monkeypatch, cloud_ids=("a", "b", "c"), cap=1)
    assert rig.drain.tick() == "ran:vocals"
    first = rig.source.fetched[0]
    rig.protected.add(first)

    assert rig.drain.tick() == "ran:vocals"
    assert first in rig.bundles()                       # the deck's bundle survived
    # Control: unprotected, the cap of 1 does release the older fetched bundle.
    second = rig.source.fetched[1]
    assert rig.drain.tick() == "ran:vocals"
    assert second not in rig.bundles()


def test_a_bundle_r2_does_not_confirm_is_never_removed_by_the_drain(
    library: Library, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = CloudRig(library, cfg, tmp_path, monkeypatch, cloud_ids=("a", "b"), cap=1)
    assert rig.drain.tick() == "ran:vocals"
    first = rig.source.fetched[0]
    # The local copy now differs from what R2 holds: it is the only copy.
    (library.stems / first / "vocals.wav").write_bytes(b"re-rendered locally")

    rig.cloud._release_down_to(0, rig._inputs())

    assert first in rig.bundles()


def test_a_bundle_an_eviction_pass_removed_first_does_not_break_the_release(
    library: Library, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] an eviction pass removes the drain's bundle after its R2 check [then] the release finishes and saves the ledger, [else stop].

    MUTATION TARGET: go back to a bare ``shutil.rmtree`` and the release
    raises FileNotFoundError before it saves (Claude review of #4974).
    """
    rig = CloudRig(library, cfg, tmp_path, monkeypatch, cloud_ids=("a", "b"), cap=1)
    assert rig.drain.tick() == "ran:vocals"
    first = rig.source.fetched[0]
    real = stem_cache_budget.unconfirmed_reason

    def confirm_then_lose_the_race(bundle, index):
        verdict = real(bundle, index)
        shutil.rmtree(bundle.path, ignore_errors=True)  # the timer's eviction wins
        return verdict

    monkeypatch.setattr(stem_cache_budget, "unconfirmed_reason", confirm_then_lose_the_race)
    rig.cloud._release_down_to(0, rig._inputs())

    assert first not in rig.bundles()
    assert rig.cloud._store.load() == []


def test_local_vocals_and_lyrics_run_before_any_download(
    library: Library, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lrc = "[00:01.00]line\n"
    rig = CloudRig(
        library, cfg, tmp_path, monkeypatch, cloud_ids=("cloud",),
        lrclib=Lrclib({"cloud": lrc, "local": lrc}),
    )
    audio = fx.audio_file(library.music, "local.mp3")
    fx.seed_track(library.state_db, "local", str(audio))
    fx.write_stem_bundle(library.stems, "local")

    assert rig.drain.tick() == "ran:vocals"
    assert rig.vocals_runs == ["local"] and rig.source.fetched == []
    assert [rig.drain.tick(), rig.drain.tick()] == ["ran:lyrics", "ran:lyrics"]
    assert rig.source.fetched == []
    assert rig.drain.tick() == "ran:vocals"
    assert rig.source.fetched == ["cloud"]
    # The user's own local bundle was never the drain's to release.
    assert "local" in rig.bundles()


def test_transient_ledger_refuses_a_file_it_does_not_understand(tmp_path: Path) -> None:
    path = ccv.transient_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"schema": 99, "bundles": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="schema 1"):
        ccv.TransientStore(path).load()


def test_vocals_validity_is_what_stops_the_refetch(
    library: Library, cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overshoot control for the fixed point: a vocal entry for a DIFFERENT
    audio file is not valid, so that track is fetched for again."""
    rig = CloudRig(library, cfg, tmp_path, monkeypatch, cloud_ids=("a",))
    assert rig.drain.tick() == "ran:vocals"
    rig.evict_everything()
    assert rig.drain.tick() == "green"

    shutil.copy(rig.audio["a"], rig.audio["a"].with_suffix(".bak"))
    rig.audio["a"].write_bytes(b"y" * 8192)             # the audio file was replaced
    entry = vocals_cache.cache_path(library.data_dir, "a")
    assert vocals_cache.load_valid_entry(entry, rig.audio["a"]) is None

    assert rig.drain.tick() == "ran:vocals"
    assert rig.source.fetched == ["a", "a"]
