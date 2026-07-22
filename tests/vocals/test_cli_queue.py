"""apps.vocals CLI: classification + trickle queue ordering.

Hermetic tests build throwaway state.db + master.plain.db copies of the
real schemas in tmp_path; one live smoke test runs against the real
state.db and skips cleanly when it is absent.

Regression one-liners:
  - if scan categories don't sum to total then broken
  - if a PVDI-analyzed track isn't category pvdi (even with audio gone) then broken
  - if a valid cache entry isn't category cached_demucs then broken
  - if a streaming/pathless/dead-path track isn't missing_file then broken
  - if the queue isn't (most on-disk playlist members desc, length asc) then broken
"""
from __future__ import annotations

import os
import sqlite3
import struct
import subprocess
from pathlib import Path
from typing import Any, Optional

import pytest

from apps.shared.paths import DATA_DIR
from apps.vocals import cache as vcache
from apps.vocals.cli import (
    CATEGORY_CACHED,
    CATEGORY_MISSING,
    CATEGORY_PVDI,
    CATEGORY_TODO,
    Ctx,
    _counts,
    best_playlist_rank,
    classify,
    load_tracks,
    order_todo,
    pvdi_present,
)

pytestmark = pytest.mark.requirement("CAT-05")

_PVDI_FIXED_HEADER = bytes.fromhex("0000040056220001")


# ----- synthetic fixtures ---------------------------------------------------------

def _synthetic_2ex(envelope: bytes) -> bytes:
    """Minimal PMAI container with one PVDI section (B1 layout; mirrors
    tests/webui/test_rb_vendor_units.py)."""
    section = (
        b"PVDI"
        + struct.pack(">II", 24, 24 + len(envelope))
        + _PVDI_FIXED_HEADER
        + struct.pack(">I", len(envelope))
        + envelope
    )
    head_len = 28
    header = b"PMAI" + struct.pack(">II", head_len, head_len + len(section))
    return header + b"\x00" * (head_len - len(header)) + section


def _empty_2ex() -> bytes:
    head = b"PMAI" + struct.pack(">II", 28, 28)
    return head + b"\x00" * (28 - len(head))


def _worker_result() -> dict[str, Any]:
    return {
        "schema": vcache.VOCAL_CACHE_SCHEMA, "source": "demucs-htdemucs", "fps": 2.0,
        "duration_s": 100.0, "coverage_pct": 50.0,
        "regions": [{"start_s": 0.0, "end_s": 50.0, "confidence": 0.8}],
        "params": {},
    }


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """tmp data root with state.db + master.plain.db + audio/.2EX files.

    Tracks (stable_id -> scenario):
      pvdi1    audio on disk, .2EX WITH PVDI          -> pvdi
      pvdi2    audio GONE,   .2EX WITH PVDI           -> pvdi (analysis wins)
      cachd    audio on disk, no PVDI, valid cache    -> cached_demucs
      miss1    FolderPath points nowhere              -> missing_file
      strm1    tidal: streaming row                   -> missing_file
      todoA    300 s, playlist big                    -> todo
      todoB    100 s, playlist small                  -> todo
      todoC    200 s, playlist big                    -> todo
      todoD     50 s, no playlist                     -> todo
    """
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    media = tmp_path / "media"
    media.mkdir()

    def _audio(name: str, exists: bool = True) -> str:
        p = media / f"{name}.mp3"
        if exists:
            p.write_bytes(b"audio " + name.encode())
        return str(p)

    def _twoex(name: str, payload: Optional[bytes]) -> Optional[str]:
        if payload is None:
            return None
        dat = media / f"{name}.DAT"
        (media / f"{name}.2EX").write_bytes(payload)
        dat.write_bytes(b"dat")
        return str(dat)

    vocal_2ex = _synthetic_2ex(bytes([3] * 200))
    rows = [
        # sid, length, audio path, AnalysisDataPath (.DAT sibling of .2EX)
        ("pvdi1", 250, _audio("pvdi1"), _twoex("pvdi1", vocal_2ex)),
        ("pvdi2", 250, _audio("pvdi2", exists=False), _twoex("pvdi2", vocal_2ex)),
        ("cachd", 100, _audio("cachd"), _twoex("cachd", _empty_2ex())),
        ("miss1", 200, _audio("miss1", exists=False), None),
        ("strm1", 200, "tidal:12345", None),
        ("todoA", 300, _audio("todoA"), _twoex("todoA", _empty_2ex())),
        ("todoB", 100, _audio("todoB"), None),
        ("todoC", 200, _audio("todoC"), None),
        ("todoD", 50, _audio("todoD"), None),
    ]

    state = sqlite3.connect(data / "state" / "state.db")
    state.executescript(
        """
        CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE track_vendor_ids (
            stable_id TEXT, vendor TEXT, vendor_id TEXT);
        CREATE TABLE playlists (playlist_id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE playlist_memberships (
            playlist_id TEXT, stable_id TEXT, position INTEGER);
        """
    )
    master = sqlite3.connect(data / "master.plain.db")
    master.execute(
        "CREATE TABLE djmdContent (ID TEXT, Title TEXT, Length INTEGER, "
        "FolderPath TEXT, AnalysisDataPath TEXT, rb_local_deleted INTEGER)"
    )
    for i, (sid, length, folder, adp) in enumerate(rows):
        vendor_id = f"v{i}"
        state.execute("INSERT INTO tracks VALUES (?, ?)", (sid, f"title-{sid}"))
        state.execute(
            "INSERT INTO track_vendor_ids VALUES (?, 'rekordbox', ?)",
            (sid, vendor_id),
        )
        master.execute(
            "INSERT INTO djmdContent VALUES (?, ?, ?, ?, ?, 0)",
            (vendor_id, f"title-{sid}", length, folder, adp),
        )
    # big playlist: 3 on-disk members (pvdi1, todoA, todoC); small: 1 (todoB).
    state.execute("INSERT INTO playlists VALUES ('plbig', 'big')")
    state.execute("INSERT INTO playlists VALUES ('plsml', 'small')")
    for pos, sid in enumerate(["pvdi1", "todoA", "todoC", "pvdi2"]):
        state.execute(
            "INSERT INTO playlist_memberships VALUES ('plbig', ?, ?)", (sid, pos)
        )
    state.execute("INSERT INTO playlist_memberships VALUES ('plsml', 'todoB', 0)")
    state.commit()
    state.close()
    master.commit()
    master.close()

    # valid cache entry for cachd
    audio_path = media / "cachd.mp3"
    vcache.write_entry(
        vcache.cache_path(data, "cachd"), _worker_result(), audio_path
    )
    return data


# ----- hermetic tests ----------------------------------------------------------------

def test_classification_and_counts_sum(data_dir: Path) -> None:
    ctx = Ctx(data_dir=data_dir)
    tracks = load_tracks(ctx, None)
    classify(ctx, tracks)
    by_id = {t.stable_id: t.category for t in tracks}
    assert by_id["pvdi1"] == CATEGORY_PVDI
    assert by_id["pvdi2"] == CATEGORY_PVDI, "PVDI wins even with audio gone"
    assert by_id["cachd"] == CATEGORY_CACHED
    assert by_id["miss1"] == CATEGORY_MISSING
    assert by_id["strm1"] == CATEGORY_MISSING
    for sid in ("todoA", "todoB", "todoC", "todoD"):
        assert by_id[sid] == CATEGORY_TODO
    counts = _counts(tracks)
    assert sum(counts.values()) == len(tracks) == 9


def test_queue_orders_biggest_ondisk_playlist_then_shortest(data_dir: Path) -> None:
    ctx = Ctx(data_dir=data_dir)
    tracks = load_tracks(ctx, None)
    classify(ctx, tracks)
    rank = best_playlist_rank(ctx, tracks)
    assert rank["todoA"] == (3, "big")
    assert rank["todoB"] == (1, "small")
    todo = order_todo([t for t in tracks if t.category == CATEGORY_TODO], rank)
    assert [t.stable_id for t in todo] == ["todoC", "todoA", "todoB", "todoD"], (
        "expected big-playlist members (shortest first), then small, then "
        "playlist-less"
    )


def test_playlist_filter_limits_scope(data_dir: Path) -> None:
    ctx = Ctx(data_dir=data_dir)
    tracks = load_tracks(ctx, "small")
    assert [t.stable_id for t in tracks] == ["todoB"]
    with pytest.raises(SystemExit, match="unknown playlist"):
        load_tracks(ctx, "nope")


def test_pvdi_present_probe(data_dir: Path, tmp_path: Path) -> None:
    media = data_dir.parent / "media"
    assert pvdi_present(media / "pvdi1.2EX") is True
    assert pvdi_present(media / "cachd.2EX") is False
    junk = tmp_path / "junk.2EX"
    junk.write_bytes(b"JUNK" + bytes(20))
    with pytest.raises(ValueError, match="not an ANLZ container"):
        pvdi_present(junk)


# ----- gate + race guards ---------------------------------------------------------------

def test_trickle_rejects_dry_run_plus_live() -> None:
    """[if] --dry-run and --live both passed [then] argparse SystemExit."""
    from apps.vocals.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(["trickle", "--dry-run", "--live"])


def test_process_one_refuses_audio_changed_mid_analysis(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] audio mtime changes during a worker run [then] RuntimeError,
    and NO cache entry lands (poisoned regions must never validate)."""
    from apps.vocals import cli as vcli
    ctx = Ctx(data_dir=data_dir)
    tracks = load_tracks(ctx, None)
    classify(ctx, tracks)
    tr = next(t for t in tracks if t.stable_id == "todoB")

    def _worker_that_races(audio_path: Path, timeout_s: float) -> dict[str, Any]:
        stat = audio_path.stat()
        os.utime(audio_path, (stat.st_atime, stat.st_mtime + 7))
        return _worker_result()

    monkeypatch.setattr(vcli, "run_worker", _worker_that_races)
    with pytest.raises(RuntimeError, match="changed during analysis"):
        vcli._process_one(ctx, tr, "[test]")
    assert not vcache.cache_path(data_dir, "todoB").is_file()


def test_process_one_records_pre_run_mtime(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cached audio_mtime is the PRE-worker stat, so a post-write file
    replacement invalidates the entry on next load."""
    from apps.vocals import cli as vcli
    ctx = Ctx(data_dir=data_dir)
    tracks = load_tracks(ctx, None)
    classify(ctx, tracks)
    tr = next(t for t in tracks if t.stable_id == "todoB")
    assert tr.audio_path is not None
    pre_mtime = tr.audio_path.stat().st_mtime

    monkeypatch.setattr(vcli, "run_worker", lambda _p, _timeout: _worker_result())
    _wall, entry = vcli._process_one(ctx, tr, "[test]")
    assert entry["audio_mtime"] == pre_mtime


def test_run_worker_deadline_raises_and_never_reads_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] worker exceeds deadline [then] CLI fails and subprocess has DEVNULL stdin."""
    from apps.vocals import cli as vcli
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"audio")
    seen: dict[str, Any] = {}

    def _timeout(*_args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise subprocess.TimeoutExpired("uv", 1.0)

    monkeypatch.setattr(vcli.subprocess, "run", _timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        vcli.run_worker(audio, timeout_s=1.0)
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["timeout"] == 1.0


def test_pioneer_path_cannot_escape_share_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.vocals import cli as vcli
    root = tmp_path / "share"
    root.mkdir()
    monkeypatch.setattr(vcli, "SHARE_ROOT", root)
    with pytest.raises(ValueError, match="escapes"):
        vcli._resolve_share_path("/PIONEER/../../outside.mp3")


# ----- live-data smoke (skips cleanly without local library data) ---------------------

def _real_data_dir() -> Optional[Path]:
    override = os.environ.get("VOCALS_DATA_DIR")
    candidates = (
        [Path(override)] if override
        else [DATA_DIR, Path("/Users/dev/Music/music-dj-tools/data")]
    )
    for candidate in candidates:
        if (candidate / "state" / "state.db").is_file() and (
            candidate / "master.plain.db"
        ).is_file():
            return candidate
    return None


@pytest.mark.skipif(
    _real_data_dir() is None,
    reason="real state.db/master.plain.db not present on this machine",
)
def test_scan_against_real_state_db() -> None:
    ctx = Ctx(data_dir=_real_data_dir())  # type: ignore[arg-type]
    tracks = load_tracks(ctx, None)
    classify(ctx, tracks)
    counts = _counts(tracks)
    assert sum(counts.values()) == len(tracks)
    assert len(tracks) > 0
