"""The auto-drain's analysis step: one track at a time, last, and polite.

Regression lines:
  - if the drain runs more than one analysis job per tick then broken
  - if an analysis job starts while a deck is playing or loading then broken
  - if an analysis job starts while a user-ordered job is running then broken
  - if a failing analysis job is retried in a loop instead of going terminal then broken
  - if the drain reports green while analysis is still pending then broken
  - if analysis runs before outstanding vocals or lyrics work then broken
  - if a recently loaded track is not analyzed before the rest then broken
  - if a machine-wide analysis failure burns per-track attempts then broken
  - if a drain without an analysis job behaves differently than before then broken
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from apps.lyrics.service import LyricsFetchService
from apps.webui.server import coverage_drain as cd
from apps.webui.server import coverage_outcomes as co
from apps.webui.server import coverage_recency as recency
from apps.webui.server.routes import ingest as ingest_mod
from tests.health_lights import fixtures as fx
from tests.health_lights.conftest import Library
from tests.health_lights.test_coverage_drain import LRC, Clock, Lrclib

pytestmark = pytest.mark.requirement("HEALTH-06")


class Rig:
    """A real drain over the real snapshot; the analysis job writes real rows."""

    def __init__(
        self, library: Library, *, lrclib: Lrclib | None = None, with_analysis: bool = True
    ) -> None:
        self.library = library
        self.playing = False
        self.user_jobs = False
        self.recent: list[str] = []
        self.clock = Clock()
        self.vocals_runs: list[str] = []
        self.lyrics_runs: list[str] = []
        self.analysis_runs: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.analysis_error: Exception | None = None
        service = LyricsFetchService(library.data_dir, provider=lrclib or Lrclib({}))
        self.outcomes = co.OutcomeStore(co.store_path(library.data_dir))
        jobs: dict[str, cd.JobFn] = {
            "vocals": self._vocals,
            "lyrics": cd.lyrics_job(service, self.lyrics_runs),
        }
        if with_analysis:
            jobs["analysis"] = self._analysis
        self.drain = cd.CoverageDrain(
            snapshot_fn=lambda: ingest_mod.build_snapshot(library.app),
            jobs=jobs,
            playing_fn=lambda: self.playing,
            outcomes=self.outcomes,
            config=cd.DrainConfig(cd.config_path(library.data_dir)),
            clock=self.clock,
        )
        self.drain.analysis_policy = cd.AnalysisPolicy(
            user_jobs_fn=lambda: self.user_jobs, recency_fn=lambda: self.recent
        )

    def _vocals(self, stable_id: str, audio_path: str) -> None:
        self.vocals_runs.append(stable_id)
        fx.write_vocals(self.library.vocal_cache, stable_id, Path(audio_path))

    def _analysis(self, stable_id: str, _audio_path: str) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.analysis_runs.append(stable_id)
            if self.analysis_error is not None:
                raise self.analysis_error
            fx.write_analysis_row(self.library.state_db, stable_id)
        finally:
            self.in_flight -= 1


def _covered_except_analysis(library: Library, stable_id: str) -> Path:
    audio = fx.audio_file(library.music, f"{stable_id}.mp3")
    fx.seed_track(library.state_db, stable_id, str(audio))
    fx.write_stem_bundle(library.stems, stable_id)
    fx.write_vocals(library.vocal_cache, stable_id, audio)
    fx.write_lyrics(library.lyrics_cache, stable_id)
    return audio


def _coverage(library: Library) -> dict:
    return library.client.get("/api/v1/ingest/coverage").json()


def test_analysis_jobs_are_picked_one_at_a_time_until_green(library: Library) -> None:
    for stable_id in ("a", "b", "c"):
        _covered_except_analysis(library, stable_id)
    rig = Rig(library)

    seen: list[int] = []
    outcomes: list[str] = []
    for _ in range(4):
        outcomes.append(rig.drain.tick())
        seen.append(len(rig.analysis_runs))

    assert outcomes == ["ran:analysis", "ran:analysis", "ran:analysis", "green"]
    assert seen == [1, 2, 3, 3]                     # exactly one new job per tick
    assert rig.max_in_flight == 1
    assert sorted(rig.analysis_runs) == ["a", "b", "c"]
    assert _coverage(library)["pending"]["analysis"] == 0
    assert rig.drain.status().pending["analysis"] == 0


def test_the_drain_is_not_green_while_analysis_is_pending(library: Library) -> None:
    _covered_except_analysis(library, "a")
    rig = Rig(library)

    # Vocals, stems and lyrics are all done; only analysis is outstanding.
    assert _coverage(library)["pending"] == {"analysis": 1, "stems": 0, "vocals": 0, "lyrics": 0}
    assert rig.drain.tick() == "ran:analysis"
    assert rig.drain.tick() == "green"
    assert rig.drain.tick() == "green"
    assert rig.analysis_runs == ["a"]               # overshoot control: done means stop


def test_no_analysis_job_starts_during_playback(library: Library) -> None:
    _covered_except_analysis(library, "a")
    rig = Rig(library)
    rig.playing = True

    assert rig.drain.tick() == "paused_playing"
    assert rig.drain.tick() == "paused_playing"
    assert rig.analysis_runs == []

    # Control: the same library does drain once the deck stops.
    rig.playing = False
    assert rig.drain.tick() == "ran:analysis"
    assert rig.analysis_runs == ["a"]


def test_analysis_yields_to_user_ordered_jobs(library: Library) -> None:
    _covered_except_analysis(library, "a")
    rig = Rig(library)
    rig.user_jobs = True

    assert rig.drain.tick() == "yielding_user_jobs"
    assert rig.analysis_runs == []
    assert rig.drain.status().state == "yielding_user_jobs"

    rig.user_jobs = False
    assert rig.drain.tick() == "ran:analysis"


def test_a_failing_analysis_job_backs_off_then_goes_terminal(library: Library) -> None:
    audio = _covered_except_analysis(library, "a")
    rig = Rig(library)
    rig.analysis_error = RuntimeError("apps.analysis.run exited 1")

    assert rig.drain.tick() == "failed:analysis"
    assert rig.drain.tick() == "blocked"            # inside the backoff: no retry
    assert rig.analysis_runs == ["a"]

    rig.clock.now += co.BACKOFF_BASE_S
    assert rig.drain.tick() == "failed:analysis"
    rig.clock.now += co.BACKOFF_BASE_S * 2
    assert rig.drain.tick() == "failed:analysis"
    assert rig.analysis_runs == ["a"] * co.MAX_ATTEMPTS

    rig.clock.now += 10**9                          # terminal: time does not re-arm it
    assert rig.drain.tick() == "blocked"
    assert rig.drain.tick() == "blocked"
    assert rig.analysis_runs == ["a"] * co.MAX_ATTEMPTS
    coverage = _coverage(library)
    assert coverage["failed"]["analysis"] == 1
    assert coverage["pending"]["analysis"] == 0

    # A changed audio file is a changed failure: the track is tried again
    # (after its vocals, which the new audio made stale too).
    audio.write_bytes(b"z" * 8192)
    rig.analysis_error = None
    assert [rig.drain.tick() for _ in range(3)] == ["ran:vocals", "ran:analysis", "green"]


def test_a_machine_wide_analysis_failure_stops_the_step_without_burning_attempts(
    library: Library,
) -> None:
    for stable_id in ("a", "b"):
        _covered_except_analysis(library, stable_id)
    rig = Rig(library)
    rig.analysis_error = cd.StepUnavailable("the 'librosa' backend is not installed here")

    assert rig.drain.tick() == "blocked"
    assert rig.drain.tick() == "blocked"
    assert len(rig.analysis_runs) == 1              # not once per track, not once per tick
    assert rig.outcomes.load() == {}                # no per-track failure recorded
    status = rig.drain.status()
    assert "librosa" in status.unavailable_steps["analysis"]
    assert "analysis cannot run here" in (status.reason or "")

    # ``retry`` re-arms the step once the machine is fixed.
    rig.analysis_error = None
    rig.drain.retry_failed()
    assert rig.drain.tick() == "ran:analysis"


def test_analysis_runs_after_vocals_and_lyrics(library: Library) -> None:
    audio = fx.audio_file(library.music, "a.mp3")
    fx.seed_track(library.state_db, "a", str(audio))
    fx.write_stem_bundle(library.stems, "a")
    rig = Rig(library, lrclib=Lrclib({"a": LRC}))

    assert [rig.drain.tick() for _ in range(4)] == [
        "ran:vocals", "ran:lyrics", "ran:analysis", "green",
    ]


def test_recently_loaded_tracks_are_analyzed_first(library: Library) -> None:
    for stable_id in ("a", "b", "c", "d"):
        _covered_except_analysis(library, stable_id)
    rig = Rig(library)
    rig.recent = ["c", "not-in-library", "b"]       # most recent first

    for _ in range(4):
        rig.drain.tick()

    assert rig.analysis_runs == ["c", "b", "a", "d"]


def test_control_a_drain_without_an_analysis_job_ignores_analysis(library: Library) -> None:
    """Vocals/lyrics behavior is unchanged: same outcomes, same order, same green."""
    for stable_id in ("a", "b"):
        audio = fx.audio_file(library.music, f"{stable_id}.mp3")
        fx.seed_track(library.state_db, stable_id, str(audio))
        fx.write_stem_bundle(library.stems, stable_id)
    rig = Rig(library, lrclib=Lrclib({"a": LRC, "b": None}), with_analysis=False)
    rig.user_jobs = True                            # must not touch vocals or lyrics
    rig.recent = ["b"]                              # must not reorder vocals or lyrics

    outcomes = [rig.drain.tick() for _ in range(5)]

    assert outcomes == ["ran:vocals", "ran:vocals", "ran:lyrics", "ran:lyrics", "green"]
    assert rig.vocals_runs == ["a", "b"]
    assert rig.lyrics_runs == ["a", "b"]
    assert rig.analysis_runs == []
    assert _coverage(library)["pending"]["analysis"] == 2


def test_stages_that_need_the_farm_are_reported_never_run(library: Library) -> None:
    _covered_except_analysis(library, "a")
    fx.write_analysis_row(library.state_db, "a")
    rig = Rig(library)
    rig.drain.analysis_policy = cd.AnalysisPolicy(
        farm_only_stages={"own_beatgrid.backfill": "needs torch"},
        farm_pending_fn=lambda present: {"own_beatgrid.backfill": len(present)},
    )

    assert rig.drain.tick() == "green"
    status = rig.drain.status()
    assert status.farm_only_stages == {"own_beatgrid.backfill": "needs torch"}
    assert status.farm_only_pending == {"own_beatgrid.backfill": 1}
    assert rig.analysis_runs == []


#-----------------------------------------------------------------------------
# the real pieces build_for_app wires in
#-----------------------------------------------------------------------------
def test_deck_gate_holds_through_a_load_and_releases_after_it_settles() -> None:
    clock = Clock()
    mirror: dict = {"decks": {"1": {"stable_id": None, "playing": False}}}
    gate = cd.DeckGate(lambda: mirror, clock=clock)

    assert gate() is False                          # an empty deck is not a load
    mirror["decks"]["1"]["stable_id"] = "a"
    assert gate() is True                           # a track just landed on a deck
    clock.now += cd.LOAD_SETTLE_S - 1
    assert gate() is True
    clock.now += 2
    assert gate() is False                          # settled
    mirror["decks"]["1"]["playing"] = True
    assert gate() is True                           # playing always holds
    mirror["decks"]["1"]["playing"] = False
    assert cd.DeckGate(lambda: None, clock=clock)() is False


def test_recent_track_ids_puts_loaded_decks_before_set_history(tmp_path: Path) -> None:
    sets_db = tmp_path / "sets.db"
    conn = sqlite3.connect(sets_db)
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, session_id TEXT, timestamp_s REAL, "
        "wall_clock TEXT, deck TEXT, track_stable_id TEXT, action TEXT, value_json TEXT, "
        "source TEXT)"
    )
    for wall_clock, stable_id in (
        ("2026-09-01T10:00:00Z", "old"),
        ("2026-09-30T10:00:00Z", "new"),
        ("2026-09-02T10:00:00Z", "new"),
        ("2026-09-03T10:00:00Z", None),
    ):
        conn.execute(
            "INSERT INTO events (session_id, timestamp_s, wall_clock, track_stable_id, "
            "action, source) VALUES ('s', 0, ?, ?, 'track_loaded', 'opendj')",
            (wall_clock, stable_id),
        )
    conn.commit()
    conn.close()
    mirror = {"decks": {"1": {"stable_id": "on-deck"}, "2": {"stable_id": None}}}

    assert recency.recent_track_ids(sets_db, mirror) == ["on-deck", "new", "old"]
    # No recorded set yet is a state with an answer, not an error.
    assert recency.recent_track_ids(tmp_path / "absent.db", None) == []


def test_analysis_job_classifies_real_exit_codes(tmp_path: Path) -> None:
    """Real ``apps.analysis.run`` children: no stand-in for the CLI contract."""
    data_dir = tmp_path / "data"
    fx.make_state_db(data_dir)
    gone = tmp_path / "gone.mp3"

    # A usage error (unknown backend) is about this machine, not this track.
    with pytest.raises(cd.StepUnavailable, match="exited 2"):
        cd.analysis_job(data_dir, backend="no-such-backend")("a", str(gone))
    # A vanished target is about this track: an ordinary, backed-off failure.
    with pytest.raises(RuntimeError, match="exited 3") as caught:
        cd.analysis_job(data_dir, backend="librosa")("a", str(gone))
    assert not isinstance(caught.value, cd.StepUnavailable)


def test_analysis_job_runs_one_worker_at_low_priority(tmp_path: Path) -> None:
    argv, pairs = cd.analysis_argv("a", "/music/a.mp3", backend="librosa")
    try:
        assert argv[argv.index("--workers") + 1] == "1"
        assert argv[argv.index("--backend") + 1] == "librosa"
        assert json.loads(Path(pairs).read_text(encoding="utf-8")) == [["a", "/music/a.mp3"]]
        assert "apps.webui.server.coverage_analysis_job" in argv
    finally:
        Path(pairs).unlink()
    assert cd.ANALYSIS_NICENESS >= 15
    assert cd.JOB_ORDER == ("vocals", "lyrics", "analysis")


def test_user_jobs_active_reads_the_refresh_slot_and_running_queue_items(
    library: Library,
) -> None:
    from apps.analysis.store import open_conn
    from apps.webui.server import coverage_drain_analysis as step

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(library.state_db)

    assert step.user_jobs_active(connect, lambda: False) is False   # no queue table yet
    assert step.user_jobs_active(connect, lambda: True) is True     # the refresh slot
    conn = open_conn(library.state_db)
    conn.execute(
        "INSERT INTO analysis_queue_item (batch_id, stable_id, lane, backend, file_path, "
        "state, enqueued_at) VALUES ('ub_stems', 'a', 'stems', 'user', '/a.mp3', 'pending', ?)",
        (fx.STAMP,),
    )
    conn.commit()
    assert step.user_jobs_active(connect, lambda: False) is False   # pending is not running
    conn.execute("UPDATE analysis_queue_item SET state = 'running'")
    conn.commit()
    conn.close()
    assert step.user_jobs_active(connect, lambda: False) is True


def test_farm_only_pending_counts_tracks_without_a_current_beatgrid_record(
    library: Library,
) -> None:
    from apps.analysis.backends import OWN_BEATGRID_BACKEND
    from apps.analysis_beatgrid.version import PRODUCER_VERSION
    from apps.webui.server import coverage_drain_analysis as step

    present = [("a", "/a.mp3"), ("b", "/b.mp3"), ("c", "/c.mp3")]

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(library.state_db)

    assert step.farm_only_pending(connect, present) == {OWN_BEATGRID_BACKEND: 3}
    fx.write_analysis_row(library.state_db, "a", backend=OWN_BEATGRID_BACKEND)   # old version
    fx.write_analysis_row(library.state_db, "b")                                 # librosa only
    conn = sqlite3.connect(library.state_db)
    conn.execute(
        "UPDATE analysis SET backend_version = ? WHERE stable_id = 'a'", (PRODUCER_VERSION,)
    )
    conn.execute(
        "INSERT INTO analysis SELECT 'c', backend, '1.3.0', analyzed_at, duration_s, "
        "sample_rate, bpm, bpm_confidence, key_camelot, key_openkey, key_confidence, energy, "
        "energy_source, record_json FROM analysis WHERE stable_id = 'a'"
    )
    conn.commit()
    conn.close()
    # ``a`` is current; ``b`` has no beatgrid record; ``c`` is on the old producer.
    assert step.farm_only_pending(connect, present) == {OWN_BEATGRID_BACKEND: 2}
    assert OWN_BEATGRID_BACKEND in step.FARM_ONLY_STAGES
