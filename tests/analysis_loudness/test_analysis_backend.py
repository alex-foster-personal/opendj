"""Registration and CLI round-trip for own_loudness.backfill.

[if] own_loudness.backfill cannot be selected or cannot write a record [then] fail, [else stop].
"""

from __future__ import annotations

import os
import pickle
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from apps.analysis.backends import get_backend
from apps.analysis.backends.base import (
    AnalyzerBackend,
    TrackUnreadable,
    TrackVanished,
)
from apps.analysis.backends.own_loudness import _STDERR_TAIL, _run_with_bounded_stderr
from apps.analysis.record import AnalysisRecord
from apps.analysis_loudness import PRODUCER_BACKEND, PRODUCER_VERSION, analyze_file

pytestmark = pytest.mark.requirement("NATIVE-07")

REPO_ROOT = Path(__file__).resolve().parents[2]
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_LUFS_TOLERANCE = 0.1
_DBTP_TOLERANCE = 0.1


def _plain(text: str) -> str:
    """Strip Rich markup so CLI assertions match the words, not the styling."""
    return _ANSI_RE.sub("", text)


def _require_ffmpeg() -> None:
    import shutil

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required to run NATIVE-07 acceptance tests")


def _sine(path: Path) -> Path:
    _require_ffmpeg()
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.1*sin(2*PI*1000*t):d=5:s=44100",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_get_backend_resolves_own_loudness_backfill() -> None:
    """[if] the registry cannot resolve own_loudness.backfill [then] fail."""
    cls = get_backend("own_loudness.backfill")
    assert cls.name == PRODUCER_BACKEND == "own_loudness.backfill"
    assert cls.version == PRODUCER_VERSION == "1.0.0"


def test_own_loudness_resolves_without_importing_other_backends() -> None:
    """[if] resolving loudness pulls librosa/madmom/mik [then] fail."""
    code = (
        "import sys\n"
        "from apps.analysis.backends import get_backend\n"
        "cls = get_backend('own_loudness.backfill')\n"
        "print(cls.name)\n"
        "print(cls.version)\n"
        "others = [n for n in (\n"
        "    'apps.analysis.backends.librosa',\n"
        "    'apps.analysis.backends.librosa_madmom',\n"
        "    'apps.analysis.backends.mik',\n"
        ") if n in sys.modules]\n"
        "print('others=' + ','.join(others))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line]
    assert lines[0] == "own_loudness.backfill"
    assert lines[1] == "1.0.0"
    assert lines[-1] == "others="


def test_backend_satisfies_analyzer_protocol_and_is_picklable() -> None:
    """[if] the wrapper is not a spawn-picklable AnalyzerBackend [then] fail."""
    cls = get_backend("own_loudness.backfill")
    assert isinstance(cls, AnalyzerBackend)
    assert cls.jit_cache_roots() == ()
    warm = cls.warm_jit_cache()
    assert "no in-process JIT" in warm
    assert "ffmpeg" in warm
    assert pickle.loads(pickle.dumps(cls)) is cls


def test_cli_writes_v2_record_and_projects_loudness(tmp_path: Path) -> None:
    """[if] the CLI cannot persist projected LUFS/dBTP for one track [then] fail."""
    wav = _sine(tmp_path / "target.wav")
    data_dir = tmp_path / "data"
    env = {**os.environ, "MDT_DATA_DIR": str(data_dir)}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.analysis.run",
            "--backend",
            "own_loudness.backfill",
            "--workers",
            "1",
            "--all",
            "--files",
            str(wav),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    stdout = _plain(proc.stdout)
    assert "backend=own_loudness.backfill" in stdout
    assert "no in-process JIT" in stdout
    assert "analysed=1" in stdout
    assert "new" in stdout

    db_path = data_dir / "state" / "state.db"
    assert db_path.is_file()
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT backend, backend_version, record_json FROM analysis").fetchone()
        assert row is not None
        backend, backend_version, record_json = row
        rec = AnalysisRecord.from_json(record_json)
        assert rec.backend == backend == "own_loudness.backfill"
        assert rec.producer == "backfill"
        assert rec.backend_version == rec.producer_version == backend_version
        assert rec.backend_version == PRODUCER_VERSION
        assert rec.uses_model is False
        assert rec.model_sha256 is None
        assert _SHA256_RE.match(rec.decode_fingerprint)
        assert rec.sample_rate == 44100
        assert rec.duration_s == pytest.approx(5.0, abs=0.05)
        assert set(rec.lanes) == {"loudness"}
        assert rec.lanes["loudness"].status == "ok"

        pointer = conn.execute(
            "SELECT backend, backend_version FROM analysis_canonical "
            "WHERE stable_id = ? AND lane = 'loudness'",
            (rec.stable_id,),
        ).fetchone()
        assert pointer == ("own_loudness.backfill", PRODUCER_VERSION)

        proj = {
            field: value
            for field, value in conn.execute(
                "SELECT field, value FROM analysis_projection WHERE stable_id = ?",
                (rec.stable_id,),
            )
        }
        assert set(proj) == {"loudness_lufs", "loudness_dbtp"}
        direct = analyze_file(wav)
        assert proj["loudness_lufs"] == pytest.approx(direct.integrated_lufs, abs=_LUFS_TOLERANCE)
        assert proj["loudness_dbtp"] == pytest.approx(direct.true_peak_dbtp, abs=_DBTP_TOLERANCE)
        assert rec.lanes["loudness"].payload["integrated_lufs"] == pytest.approx(
            direct.integrated_lufs, abs=_LUFS_TOLERANCE
        )
        assert rec.lanes["loudness"].payload["true_peak_dbtp"] == pytest.approx(
            direct.true_peak_dbtp, abs=_DBTP_TOLERANCE
        )

        eav = conn.execute(
            "SELECT count(*) FROM analysis_projection WHERE field = 'loudness'"
        ).fetchone()[0]
        assert eav == 0
    finally:
        conn.close()


def test_missing_ffmpeg_is_backend_not_available(tmp_path: Path) -> None:
    """[if] missing ffmpeg fabricates a record [then] fail."""
    wav = _sine(tmp_path / "input.wav")
    code = (
        "from pathlib import Path\n"
        "from apps.analysis.backends import get_backend\n"
        "from apps.analysis.backends.base import BackendNotAvailable\n"
        "cls = get_backend('own_loudness.backfill')\n"
        "try:\n"
        f"    cls.analyze(Path({str(wav)!r}), 'sid')\n"
        "except BackendNotAvailable as exc:\n"
        "    print('BackendNotAvailable')\n"
        "    print(exc)\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit('expected BackendNotAvailable')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PATH": ""},
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "BackendNotAvailable" in proc.stdout
    assert "not on PATH" in proc.stdout


def test_missing_target_is_track_vanished() -> None:
    """[if] a missing path is not TrackVanished [then] fail."""
    cls = get_backend("own_loudness.backfill")
    missing = Path("/no/such/own-loudness-target.wav")
    assert not missing.exists()
    with pytest.raises(TrackVanished):
        cls.analyze(missing, "sid-vanished")


def test_present_undecodable_file_is_track_unreadable(tmp_path: Path) -> None:
    """[if] a present non-audio file is not TrackUnreadable [then] fail."""
    junk = tmp_path / "not-audio.wav"
    junk.write_bytes(b"this is not an audio file")
    cls = get_backend("own_loudness.backfill")
    with pytest.raises(TrackUnreadable):
        cls.analyze(junk, "sid-unreadable")


def test_fingerprint_stderr_is_ring_buffered_not_captured_whole() -> None:
    """[if] decoder stderr is retained unbounded [then] fail."""
    source = (REPO_ROOT / "apps/analysis/backends/own_loudness.py").read_text()
    assert "capture_output=" not in source
    assert "communicate(" not in source
    payload = ("N" * 200_000) + "UNIQUE-TAIL"
    code = (
        "import sys\n"
        "sys.stderr.write('N' * 200000)\n"
        "sys.stderr.write('UNIQUE-TAIL')\n"
        "sys.stderr.flush()\n"
        "raise SystemExit(3)\n"
    )
    returncode, tail = _run_with_bounded_stderr(
        [sys.executable, "-c", code],
        timeout_s=15,
    )
    assert returncode == 3
    encoded = tail.encode("utf-8")
    assert len(encoded) == _STDERR_TAIL
    assert tail == payload[-_STDERR_TAIL:]
    assert tail.endswith("UNIQUE-TAIL")
