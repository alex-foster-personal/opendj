"""Stems that can never exist are a recordable, clearable finished state (HEALTH-09).

Regression lines:
  - if a vocal-stem file stays pending for stems forever then broken
  - if a source ffprobe cannot read a duration from stays pending forever then broken
  - if a 12 s track is marked too short, or a 2 s one is not, then broken
  - if a file that merely cannot be opened is marked terminal then broken
  - if "Artist - Song - Instrumental" is marked as a stem file then broken
  - if a cleared mark is put straight back by the automatic check then broken
  - if a marked track that later gains a bundle is not counted done then broken
  - if a mark survives the audio file being replaced then broken

[if] a track that can never have stems stays pending forever [then] fail, [else stop].
"""
from __future__ import annotations

import shutil
import wave
from pathlib import Path

import pytest

from apps.webui import coverage_drain_cli as cli
from apps.webui.server import coverage_drain as cd
from apps.webui.server import coverage_outcomes as co
from apps.webui.server import coverage_stems_terminal as st
from apps.webui.server.routes import ingest as ingest_mod
from tests.health_lights import fixtures as fx
from tests.health_lights.conftest import Library
from tests.health_lights.test_coverage_drain import Clock

pytestmark = pytest.mark.requirement("HEALTH-09")

needs_ffprobe = pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="ffprobe is not installed on this machine"
)
NO_SOURCE = "/api/v1/coverage-outcomes/stems/no-source"


def _wav(path: Path, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\x00\x00" * int(8_000 * seconds))
    return path


def _track(library: Library, stable_id: str, audio: Path) -> Path:
    fx.seed_track(library.state_db, stable_id, str(audio))
    return audio


def _coverage(library: Library) -> dict:
    return library.client.get("/api/v1/ingest/coverage").json()


def _stores(library: Library) -> tuple[co.OutcomeStore, st.KeepPending]:
    return (
        co.OutcomeStore(co.store_path(library.data_dir)),
        st.KeepPending(st.keep_pending_path(library.data_dir)),
    )


def _drain(library: Library, *, probe_fn=st.probe) -> tuple[cd.CoverageDrain, st.StemsCheck]:
    outcomes, keep = _stores(library)
    check = st.StemsCheck(
        outcomes=outcomes, keep=keep, title_fn=lambda _stable_id: None, probe_fn=probe_fn
    )
    drain = cd.CoverageDrain(
        snapshot_fn=lambda: ingest_mod.build_snapshot(library.app),
        jobs={}, playing_fn=lambda: False, outcomes=outcomes,
        config=cd.DrainConfig(cd.config_path(library.data_dir)),
        clock=Clock(), stems_check=check,
    )
    return drain, check


# --- the rules ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "title", "is_stem"),
    [
        ("/m/acapellas/vocals/Song.mp3", None, True),
        ("/m/packs/Instrumental/Song.mp3", None, True),
        ("/m/Artist - Song - vocals.mp3", None, True),
        ("/m/041_abc123-vocals.mp3", None, True),
        ("/m/song_VOCALS.wav", None, True),
        ("/m/plain.mp3", "041_abc123-vocals", True),
        # Controls: ordinary releases that DO separate into stems.
        ("/m/Artist - Song - Instrumental.mp3", "Song (Instrumental)", False),
        ("/m/Artist - Vocals Only Dub.mp3", "Vocals", False),
        ("/m/vocalsong.mp3", "My Vocals Are Loud", False),
        ("/m/vocals/sub/Song.mp3", None, False),
    ],
)
def test_stem_file_rule_matches_names_not_lookalikes(path: str, title, is_stem: bool) -> None:
    assert (st.stem_file_reason(Path(path), title) is not None) is is_stem


@pytest.mark.parametrize(
    ("probe", "terminal"),
    [
        (st.Probe("no_duration", detail="ffprobe reported duration None"), True),
        (st.Probe("duration", duration_s=2.0), True),
        (st.Probe("duration", duration_s=9.99), True),
        (st.Probe("duration", duration_s=10.0), False),
        (st.Probe("duration", duration_s=12.0), False),
        (st.Probe("unknown", detail="cannot open the file"), False),
    ],
)
def test_classify_thresholds_and_unknown_is_never_terminal(probe: st.Probe, terminal: bool) -> None:
    reason = st.classify(Path("/m/plain.mp3"), None, probe_fn=lambda _path: probe)
    assert (reason is not None) is terminal


def test_a_stem_file_is_classified_without_running_ffprobe() -> None:
    def explode(_path: Path) -> st.Probe:
        raise AssertionError("ffprobe must not run for a file named as a stem")

    assert "vocal stem" in str(st.classify(Path("/m/x - vocals.mp3"), None, probe_fn=explode))


@needs_ffprobe
def test_real_ffprobe_durations_and_damage(tmp_path: Path) -> None:
    short = st.probe(_wav(tmp_path / "short.wav", 2.0))
    long = st.probe(_wav(tmp_path / "long.wav", 12.0))
    assert short.kind == "duration" and short.duration_s == pytest.approx(2.0, abs=0.05)
    assert long.kind == "duration" and long.duration_s == pytest.approx(12.0, abs=0.05)
    damaged = tmp_path / "damaged.mp3"
    damaged.write_bytes(b"\x00" * 4096)
    damaged_probe = st.probe(damaged)
    assert damaged_probe.kind == "no_duration", damaged_probe.detail
    # A file that is not there is UNKNOWN (an unmounted volume), never damage.
    assert st.probe(tmp_path / "absent.mp3").kind == "unknown"


def test_missing_ffprobe_is_unknown_and_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(st.shutil, "which", lambda _name: None)
    audio = _wav(tmp_path / "x.wav", 2.0)
    assert st.probe(audio) == st.Probe("unknown", detail="ffprobe is not installed")
    assert "ffprobe is not installed" in str(st.ffprobe_refusal())
    assert st.classify(audio, None) is None


def _ffprobe_that_fails_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str) -> None:
    """Put an executable named ffprobe first on PATH that exits 1 after printing
    `line` on stderr, with `{path}` replaced by its last argument."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "ffprobe"
    template = line.replace("{path}", "${path}")
    script.write_text(f'#!/bin/sh\nfor path; do :; done\necho "{template}" >&2\nexit 1\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))


def test_an_older_ffprobe_invalid_argument_for_the_file_is_damage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffprobe before FFmpeg 7 names a frameless file `<path>: Invalid argument`."""
    damaged = tmp_path / "damaged.mp3"
    damaged.write_bytes(b"\x00" * 4096)
    _ffprobe_that_fails_with(tmp_path, monkeypatch, "{path}: Invalid argument")
    assert st.probe(damaged) == st.Probe("no_duration", detail="ffprobe: invalid data")


@pytest.mark.parametrize(
    "line",
    ["{path}: Permission denied", "{path}: Input/output error", "Invalid argument"],
)
def test_any_other_ffprobe_failure_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    """Overshoot control: only the demuxer's own rejection of this file is damage."""
    damaged = tmp_path / "damaged.mp3"
    damaged.write_bytes(b"\x00" * 4096)
    _ffprobe_that_fails_with(tmp_path, monkeypatch, line)
    result = st.probe(damaged)
    assert result.kind == "unknown" and "ffprobe exited 1" in str(result.detail)


# --- the drain-driven check -------------------------------------------------------


@needs_ffprobe
def test_drain_marks_terminal_stems_and_the_lights_count_them_finished(library: Library) -> None:
    _track(library, "acapella", _wav(library.music / "Artist - Song - vocals.wav", 30.0))
    _track(library, "oneshot", _wav(library.music / "oneshot.wav", 2.0))
    damaged = library.music / "damaged.mp3"
    damaged.write_bytes(b"\x00" * 4096)
    _track(library, "damaged", damaged)
    _track(library, "real", _wav(library.music / "real.wav", 12.0))
    before = _coverage(library)
    assert before["pending"]["stems"] == 4 and before["terminal"]["stems"] == 0
    drain, check = _drain(library)

    assert drain.tick() == "ran:stems-check"

    after = _coverage(library)
    assert after["terminal"]["stems"] == 3
    # Overshoot control: the ordinary 12 s track is still pending for the farm.
    assert after["pending"]["stems"] == 1
    assert after["terminal"]["vocals"] == 3 and after["waiting_on_stems"] == 1
    outcomes, _keep = _stores(library)
    reasons = {o.stable_id: o.reason for o in st.list_no_source(outcomes)}
    assert reasons["acapella"].startswith("auto: the file is itself a vocal stem")
    assert "under the 10 s minimum" in reasons["oneshot"]
    assert "unreadable" in reasons["damaged"]
    assert "real" not in reasons
    # The check does not re-probe what it has seen: the next tick moves on.
    assert drain.tick() == "blocked"
    assert drain.status().stems_no_source == 3
    assert drain.status().stems_needing_farm == ["real"]
    assert check.marked_total == 3


def test_check_is_skipped_while_the_cloud_index_is_unknown(library: Library) -> None:
    """Unknown index: a missing bundle may be in the cloud, so nothing is
    classified, and nothing is reported as needing the farm."""
    _track(library, "acapella", _wav(library.music / "x - vocals.wav", 30.0))
    library.app.state.stem_hydration_unarmed_reason = "hub enrollment expired"
    drain, check = _drain(library)

    assert drain.tick() == "blocked"

    assert check.marked_total == 0
    status = drain.status()
    assert status.stems_unclassified == 1 and status.stems_needing_farm_count == 0
    assert "cannot say whether one exists" in str(status.reason)


# --- manual marks: route, CLI, clear ------------------------------------------------


def test_mark_list_clear_round_trip_over_http(library: Library) -> None:
    _track(library, "t", _wav(library.music / "t.wav", 12.0))
    client = library.client

    marked = client.post(NO_SOURCE, json={"stable_id": "t", "reason": "DJ tool, no mix"})
    assert marked.status_code == 200, marked.text
    assert marked.json()["reason"] == "manual: DJ tool, no mix"
    assert marked.json()["applies"] is True
    assert _coverage(library)["terminal"]["stems"] == 1
    assert _coverage(library)["pending"]["stems"] == 0
    listed = client.get(NO_SOURCE).json()
    assert [m["stable_id"] for m in listed["marks"]] == ["t"]

    cleared = client.delete(f"{NO_SOURCE}/t")
    assert cleared.json() == {"stable_id": "t", "cleared": True, "keep_pending": True}
    assert _coverage(library)["terminal"]["stems"] == 0
    assert _coverage(library)["pending"]["stems"] == 1
    assert client.get(NO_SOURCE).json() == {"marks": [], "keep_pending": ["t"]}


def test_mark_refuses_an_unknown_track_and_an_empty_reason(library: Library) -> None:
    _track(library, "t", _wav(library.music / "t.wav", 12.0))
    assert library.client.post(NO_SOURCE, json={"stable_id": "nope", "reason": "x"}).status_code == 404
    assert library.client.post(NO_SOURCE, json={"stable_id": "t", "reason": ""}).status_code == 422
    assert library.client.post(NO_SOURCE, json={"stable_id": "t"}).status_code == 422
    assert _coverage(library)["terminal"]["stems"] == 0


def test_a_cleared_automatic_mark_stays_cleared_until_marked_again(library: Library) -> None:
    _track(library, "acapella", _wav(library.music / "x - vocals.wav", 30.0))
    drain, _check = _drain(library)
    assert drain.tick() == "ran:stems-check"
    assert _coverage(library)["terminal"]["stems"] == 1

    assert library.client.delete(f"{NO_SOURCE}/acapella").json()["cleared"] is True
    # A FRESH check (an engine restart forgets the in-memory seen set) must
    # still leave it alone: the keep-pending file is what holds.
    fresh_drain, fresh_check = _drain(library)
    fresh_drain.tick()
    assert fresh_check.marked_total == 0
    assert _coverage(library)["pending"]["stems"] == 1

    # Marking it by hand again removes the hold.
    library.client.post(NO_SOURCE, json={"stable_id": "acapella", "reason": "it is a stem"})
    assert library.client.get(NO_SOURCE).json()["keep_pending"] == []
    assert _coverage(library)["terminal"]["stems"] == 1


def test_a_marked_track_that_gains_a_bundle_counts_done_local(library: Library) -> None:
    _track(library, "t", _wav(library.music / "t.wav", 12.0))
    library.client.post(NO_SOURCE, json={"stable_id": "t", "reason": "thought it was a stem"})
    assert _coverage(library)["terminal"]["stems"] == 1

    fx.write_stem_bundle(library.stems, "t")

    body = _coverage(library)
    assert body["done"]["stems"] == 1 and body["local"]["stems"] == 1
    assert body["terminal"]["stems"] == 0 and body["pending"]["stems"] == 0


def test_a_mark_does_not_survive_the_audio_file_being_replaced(library: Library) -> None:
    audio = _track(library, "t", _wav(library.music / "t.wav", 12.0))
    library.client.post(NO_SOURCE, json={"stable_id": "t", "reason": "damaged rip"})
    assert _coverage(library)["terminal"]["stems"] == 1

    _wav(audio, 40.0)      # re-ripped: a different size, so a different signature

    assert _coverage(library)["terminal"]["stems"] == 0
    assert _coverage(library)["pending"]["stems"] == 1
    assert library.client.get(NO_SOURCE).json()["marks"][0]["applies"] is False


def test_cli_verbs_build_the_same_requests_as_the_routes() -> None:
    assert cli.request_for("no-source-list") == ("GET", NO_SOURCE, None)
    assert cli.request_for("no-source-mark", stable_id="a b", reason="why") == (
        "POST", NO_SOURCE, {"stable_id": "a b", "reason": "why"},
    )
    assert cli.request_for("no-source-clear", stable_id="a/b") == (
        "DELETE", f"{NO_SOURCE}/a%2Fb", None,
    )
    with pytest.raises(ValueError, match="--stable-id, --reason"):
        cli.request_for("no-source-mark")
