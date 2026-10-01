"""Tag write-back tests (META-01).  MP3 / FLAC round-trip, M4A refusal + safety rails."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.analysis import write_tags as wt
from apps.analysis.record import AnalysisRecord


def _rec(sid: str) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=sid,
        backend="librosa+madmom",
        backend_version="librosa==0.10.2+madmom==0.17.dev",
        analyzed_at=datetime(2026, 4, 17, tzinfo=UTC),
        duration_s=10.0, sample_rate=44100,
        bpm=128.12, bpm_confidence=0.9,
        key_camelot="8A", key_openkey="1m", key_confidence=0.9,
        energy=7,
    )


@pytest.mark.requirement("META-01")
def test_build_new_tags_namespace() -> None:
    tags = wt._build_new_tags(_rec("x"))
    for k in ("BPM", "INITIALKEY", "OPENDJ_ENERGY",
              "OPENDJ_ENERGY_SOURCE", "OPENDJ_BACKEND_VERSION"):
        assert k in tags


@pytest.mark.requirement("META-01")
def test_container_detection(tmp_path: Path) -> None:
    assert wt._container_kind(tmp_path / "a.mp3") == "mp3"
    assert wt._container_kind(tmp_path / "a.m4a") == "mp4"
    assert wt._container_kind(tmp_path / "a.flac") == "flac"
    assert wt._container_kind(tmp_path / "a.ogg") == "ogg"
    with pytest.raises(ValueError):
        wt._container_kind(tmp_path / "a.wav")


@pytest.mark.requirement("META-01")
def test_flac_roundtrip(flac_fixture: Path) -> None:
    new = wt._build_new_tags(_rec("f"))
    wt._write_tags(flac_fixture, new)
    got = wt._read_current_tags(flac_fixture)
    for k, v in new.items():
        assert got[k] == v


@pytest.mark.requirement("META-01")
def test_mp3_roundtrip(mp3_fixture: Path) -> None:
    new = wt._build_new_tags(_rec("m"))
    wt._write_tags(mp3_fixture, new)
    got = wt._read_current_tags(mp3_fixture)
    assert got["INITIALKEY"] == "8A"
    assert got["OPENDJ_ENERGY"] == "7"
    assert abs(float(got["BPM"]) - float(new["BPM"])) < 0.5


@pytest.mark.requirement("TAGIO-03")
def test_m4a_write_is_refused_and_the_planner_skips_it(m4a_fixture: Path) -> None:
    """[if] write-back targets an .m4a [then] it is refused, file unchanged, [else stop]."""
    before = m4a_fixture.read_bytes()
    with pytest.raises(ValueError, match="not supported"):
        wt._write_tags(m4a_fixture, wt._build_new_tags(_rec("mp4")))
    assert wt.plan_deltas([_rec("mp4")], file_map={"mp4": m4a_fixture}) == []
    assert m4a_fixture.read_bytes() == before


@pytest.mark.requirement("META-01")
def test_dry_run_no_mutation(flac_fixture: Path) -> None:
    delta = wt.TagDelta(
        path=flac_fixture, stable_id="dr", old={},
        new=wt._build_new_tags(_rec("dr")),
    )
    s = wt.apply_writes([delta], live=False, bulk=False)
    assert s.would_write == 1
    assert s.written == 0
    assert "OPENDJ_ENERGY" not in wt._read_current_tags(flac_fixture)


@pytest.mark.requirement("META-01")
def test_live_cautious_ok(
    flac_fixture: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wt, "BACKUP_ROOT", tmp_path / "backups")
    monkeypatch.setattr(wt, "REVERSAL_ROOT", tmp_path / "reversal")
    delta = wt.TagDelta(
        path=flac_fixture, stable_id="live",
        old=wt._read_current_tags(flac_fixture),
        new=wt._build_new_tags(_rec("live")),
    )
    s = wt.apply_writes([delta], live=True, bulk=False)
    assert s.written == 1 and s.failed == 0
    assert len(s.backups) == 1 and s.backups[0].exists()
    assert len(s.reversal_scripts) == 1
    payload = json.loads(s.backups[0].read_text("utf-8"))
    assert payload["stable_id"] == "live"
    assert payload["path"] == str(flac_fixture)
    assert wt._read_current_tags(flac_fixture)["INITIALKEY"] == "8A"


@pytest.mark.requirement("META-01")
def test_cautious_caps_at_max(flac_fixture: Path, tmp_path: Path) -> None:
    deltas = []
    for i in range(5):
        p = tmp_path / f"c{i}.flac"
        shutil.copy2(flac_fixture, p)
        deltas.append(wt.TagDelta(
            path=p, stable_id=f"sid{i}", old={},
            new=wt._build_new_tags(_rec(f"s{i}")),
        ))
    with pytest.raises(SystemExit) as exc:
        wt.apply_writes(deltas, live=True, bulk=False)
    assert "cautious" in str(exc.value).lower()


@pytest.mark.requirement("META-01")
def test_bulk_requires_token(flac_fixture: Path, tmp_path: Path) -> None:
    deltas = []
    for i in range(4):
        p = tmp_path / f"b{i}.flac"
        shutil.copy2(flac_fixture, p)
        deltas.append(wt.TagDelta(
            path=p, stable_id=f"B{i}", old={},
            new=wt._build_new_tags(_rec(f"B{i}")),
        ))
    with pytest.raises(SystemExit) as exc:
        wt.apply_writes(deltas, live=True, bulk=True, confirm_token="wrong")
    assert "WRITE TAGS TO 4 FILES" in str(exc.value)


@pytest.mark.requirement("META-01")
def test_reversal_restores_old_tags(
    flac_fixture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wt, "BACKUP_ROOT", tmp_path / "backups")
    monkeypatch.setattr(wt, "REVERSAL_ROOT", tmp_path / "reversal")
    wt._write_tags(flac_fixture, {
        "BPM": "100.00", "INITIALKEY": "1A",
        "OPENDJ_ENERGY": "3",
        "OPENDJ_ENERGY_SOURCE": "inferred",
        "OPENDJ_BACKEND_VERSION": "legacy",
    })
    old = wt._read_current_tags(flac_fixture)

    delta = wt.TagDelta(
        path=flac_fixture, stable_id="rev",
        old=old, new=wt._build_new_tags(_rec("rev")),
    )
    s = wt.apply_writes([delta], live=True, bulk=False)
    script = s.reversal_scripts[0]

    env = {"PYTHONPATH": str(Path.cwd()), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    got = wt._read_current_tags(flac_fixture)
    assert got["INITIALKEY"] == "1A"
    assert got["OPENDJ_ENERGY"] == "3"


@pytest.mark.requirement("META-01")
def test_live_without_i_understand_aborts(flac_fixture: Path) -> None:
    with pytest.raises(SystemExit):
        wt.main(["--live", "--files", str(flac_fixture)])
