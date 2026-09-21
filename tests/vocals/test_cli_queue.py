"""apps.vocals CLI: classification + trickle queue ordering.

Hermetic tests build throwaway state.db + master.plain.db copies of the
real schemas in tmp_path; one live smoke test runs against the real
state.db and skips cleanly when it is absent.

Regression one-liners:
  - if scan categories don't sum to total then broken
  - if a PVDI-analyzed track isn't category pvdi (even with audio gone) then broken
  - if a valid cache entry isn't category cached_demucs then broken
  - if a streaming/pathless/dead-path track isn't missing_file then broken
  - if an audio track without a servable ANLZ .DAT reaches the trickle queue then broken
  - if a /PIONEER ANLZ symlink escapes the share root but reaches the queue then broken
  - if `one` accepts cached audio without a servable ANLZ .DAT then broken
  - if `one --force` accepts an ANLZ symlink escape then broken
  - if the queue isn't (most on-disk playlist members desc, length asc) then broken
"""

from __future__ import annotations

import os
import signal
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from apps.shared.paths import DATA_DIR
from apps.vocals import cache as vcache
from apps.vocals.cli import (
    CATEGORY_CACHED,
    CATEGORY_MISSING,
    CATEGORY_MISSING_ANALYSIS,
    CATEGORY_PVDI,
    CATEGORY_TODO,
    Ctx,
    _counts,
    best_playlist_rank,
    classify,
    load_tracks,
    main,
    order_todo,
    pvdi_present,
)
from apps.vocals.errors import UnknownPlaylistError

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
        "schema": vcache.VOCAL_CACHE_SCHEMA,
        "source": "demucs-htdemucs",
        "fps": 2.0,
        "duration_s": 100.0,
        "coverage_pct": 50.0,
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
      noanlz    120 s, audio on disk, no ANLZ .DAT    -> missing_analysis
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

    def _twoex(name: str, payload: bytes | None) -> str | None:
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
        ("todoB", 100, _audio("todoB"), _twoex("todoB", _empty_2ex())),
        ("todoC", 200, _audio("todoC"), _twoex("todoC", _empty_2ex())),
        ("todoD", 50, _audio("todoD"), _twoex("todoD", _empty_2ex())),
        ("noanlz", 120, _audio("noanlz"), None),
    ]

    state = sqlite3.connect(data / "state" / "state.db")
    state.executescript(
        """
        CREATE TABLE tracks (
            stable_id TEXT PRIMARY KEY, title TEXT, deleted_at TEXT);
        CREATE TABLE track_vendor_ids (
            stable_id TEXT, vendor TEXT, vendor_id TEXT);
        CREATE TABLE playlists (
            playlist_id TEXT PRIMARY KEY, name TEXT, deleted_at TEXT);
        CREATE TABLE playlist_memberships (
            playlist_id TEXT, stable_id TEXT, position INTEGER,
            deleted_at TEXT);
        """
    )
    master = sqlite3.connect(data / "master.plain.db")
    master.execute(
        "CREATE TABLE djmdContent (ID TEXT, Title TEXT, Length INTEGER, "
        "FolderPath TEXT, AnalysisDataPath TEXT, rb_local_deleted INTEGER)"
    )
    for i, (sid, length, folder, adp) in enumerate(rows):
        vendor_id = f"v{i}"
        state.execute(
            "INSERT INTO tracks (stable_id, title) VALUES (?, ?)",
            (sid, f"title-{sid}"),
        )
        state.execute(
            "INSERT INTO track_vendor_ids VALUES (?, 'rekordbox', ?)",
            (sid, vendor_id),
        )
        master.execute(
            "INSERT INTO djmdContent VALUES (?, ?, ?, ?, ?, 0)",
            (vendor_id, f"title-{sid}", length, folder, adp),
        )
    # big playlist: 3 on-disk members (pvdi1, todoA, todoC); small: 1 (todoB).
    state.execute(
        "INSERT INTO playlists (playlist_id, name) VALUES ('plbig', 'big')"
    )
    state.execute(
        "INSERT INTO playlists (playlist_id, name) VALUES ('plsml', 'small')"
    )
    for pos, sid in enumerate(["pvdi1", "todoA", "todoC", "pvdi2"]):
        state.execute(
            "INSERT INTO playlist_memberships (playlist_id, stable_id, position) "
            "VALUES ('plbig', ?, ?)",
            (sid, pos),
        )
    state.execute(
        "INSERT INTO playlist_memberships (playlist_id, stable_id, position) "
        "VALUES ('plsml', 'todoB', 0)"
    )
    state.commit()
    state.close()
    master.commit()
    master.close()

    # valid cache entry for cachd
    audio_path = media / "cachd.mp3"
    vcache.write_entry(vcache.cache_path(data, "cachd"), _worker_result(), audio_path)
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
    assert by_id["noanlz"] == CATEGORY_MISSING_ANALYSIS
    for sid in ("todoA", "todoB", "todoC", "todoD"):
        assert by_id[sid] == CATEGORY_TODO
    counts = _counts(tracks)
    assert sum(counts.values()) == len(tracks) == 10


def test_queue_orders_biggest_ondisk_playlist_then_shortest(data_dir: Path) -> None:
    ctx = Ctx(data_dir=data_dir)
    tracks = load_tracks(ctx, None)
    classify(ctx, tracks)
    rank = best_playlist_rank(ctx, tracks)
    assert rank["todoA"] == (3, "big")
    assert rank["todoB"] == (1, "small")
    todo = order_todo([t for t in tracks if t.category == CATEGORY_TODO], rank)
    assert [t.stable_id for t in todo] == ["todoC", "todoA", "todoB", "todoD"], (
        "expected big-playlist members (shortest first), then small, then playlist-less"
    )


def test_audio_without_anlz_dat_is_excluded_from_trickle_queue(data_dir: Path) -> None:
    """[if] a track lacks the .DAT required by /anlz [then ⛔️] it is queued."""
    ctx = Ctx(data_dir=data_dir)
    tracks = load_tracks(ctx, None)
    classify(ctx, tracks)
    todo_ids = {
        track.stable_id for track in tracks if track.category == CATEGORY_TODO
    }
    assert "noanlz" not in todo_ids


def test_cached_audio_without_anlz_dat_is_still_unservable(data_dir: Path) -> None:
    """[if] no-ANLZ audio has cached vocals [then ⛔️] it appears servable."""
    audio_path = data_dir.parent / "media" / "noanlz.mp3"
    vcache.write_entry(
        vcache.cache_path(data_dir, "noanlz"), _worker_result(), audio_path
    )

    ctx = Ctx(data_dir=data_dir)
    tracks = load_tracks(ctx, None)
    classify(ctx, tracks)
    by_id = {track.stable_id: track.category for track in tracks}

    assert by_id["noanlz"] == CATEGORY_MISSING_ANALYSIS


def test_one_rejects_cached_audio_without_anlz_dat(data_dir: Path) -> None:
    """[if] `one` sees cached no-ANLZ audio [then ⛔️] it accepts the cache."""
    audio_path = data_dir.parent / "media" / "noanlz.mp3"
    vcache.write_entry(
        vcache.cache_path(data_dir, "noanlz"), _worker_result(), audio_path
    )

    with pytest.raises(SystemExit, match=r"local ANLZ \.DAT"):
        main([
            "one",
            "--data-dir",
            str(data_dir),
            "--stable-id",
            "noanlz",
        ])


def _point_noanlz_at_share_symlink_escape(
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.shared import platform_paths

    share_root = tmp_path / "share"
    anlz_link = share_root / "PIONEER" / "USB" / "ANLZ0000.DAT"
    anlz_link.parent.mkdir(parents=True)
    outside_anlz = tmp_path / "outside" / "ANLZ0000.DAT"
    outside_anlz.parent.mkdir()
    outside_anlz.write_bytes(b"dat")
    try:
        anlz_link.symlink_to(outside_anlz)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")
    monkeypatch.setattr(platform_paths, "SHARE_ROOT", share_root)

    state = sqlite3.connect(data_dir / "state" / "state.db")
    vendor_row = state.execute(
        "SELECT vendor_id FROM track_vendor_ids WHERE stable_id = 'noanlz'"
    ).fetchone()
    state.close()
    assert vendor_row is not None

    master = sqlite3.connect(data_dir / "master.plain.db")
    master.execute(
        "UPDATE djmdContent SET AnalysisDataPath = ? WHERE ID = ?",
        ("/PIONEER/USB/ANLZ0000.DAT", vendor_row[0]),
    )
    master.commit()
    master.close()


def test_share_symlink_escaped_anlz_is_excluded_from_trickle_queue(
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] an ANLZ symlink escapes share [then ⛔️] it enters the queue."""
    _point_noanlz_at_share_symlink_escape(data_dir, tmp_path, monkeypatch)

    ctx = Ctx(data_dir=data_dir)
    tracks = load_tracks(ctx, None)
    classify(ctx, tracks)
    by_id = {track.stable_id: track.category for track in tracks}
    todo = order_todo(
        [track for track in tracks if track.category == CATEGORY_TODO],
        best_playlist_rank(ctx, tracks),
    )

    assert by_id["noanlz"] == CATEGORY_MISSING_ANALYSIS
    assert "noanlz" not in {track.stable_id for track in todo}


def test_one_force_rejects_share_symlink_escaped_anlz(
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] `one --force` sees escaped ANLZ [then ⛔️] it claims work."""
    from apps.vocals import cli as vcli

    _point_noanlz_at_share_symlink_escape(data_dir, tmp_path, monkeypatch)
    audio_path = data_dir.parent / "media" / "noanlz.mp3"
    cache_file = vcache.cache_path(data_dir, "noanlz")
    vcache.write_entry(cache_file, _worker_result(), audio_path)
    active_claim = vcli._claim_track(cache_file)
    assert active_claim is not None

    try:
        with pytest.raises(SystemExit, match=r"local ANLZ \.DAT"):
            main([
                "one",
                "--data-dir",
                str(data_dir),
                "--stable-id",
                "noanlz",
                "--force",
            ])
    finally:
        vcli._release_track_claim(active_claim)


def test_playlist_filter_limits_scope(data_dir: Path) -> None:
    ctx = Ctx(data_dir=data_dir)
    tracks = load_tracks(ctx, "small")
    assert [t.stable_id for t in tracks] == ["todoB"]
    with pytest.raises(UnknownPlaylistError, match="unknown playlist") as exc_info:
        load_tracks(ctx, "nope")
    assert exc_info.value.name == "nope"
    assert exc_info.value.known == ["big", "small"]


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


def test_trickle_requires_dry_run_or_live() -> None:
    """[if] neither --dry-run nor --live [then] argparse SystemExit."""
    from apps.vocals.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["trickle"])


def test_from_stems_requires_dry_run_or_live() -> None:
    """[if] neither --dry-run nor --live [then] argparse SystemExit."""
    from apps.vocals.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["from-stems"])


@pytest.mark.parametrize(
    ("raw_limit", "expected_limit"),
    [("-1", None), ("0", 0), ("2", 2)],
)
def test_trickle_limit_rejects_negative_and_preserves_nonnegative_values(
    raw_limit: str, expected_limit: int | None,
) -> None:
    """[if] --limit is negative [then ⛔️] parsing fails before queue work;
    [if] it is zero or positive [then] its exact value reaches trickle.

    Every case passes an explicit --dry-run: trickle requires --dry-run or
    --live (M25), so without it argparse would exit for the missing mode
    rather than for the limit, and the negative case would pass vacuously.
    """
    from apps.vocals.cli import build_parser

    argv = ["trickle", "--dry-run", "--limit", raw_limit]
    if expected_limit is None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv)
    else:
        args = build_parser().parse_args(argv)
        assert args.limit == expected_limit


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

    class TimedOutProcess:
        pid = 123456
        returncode: int | None = None

        def communicate(self, timeout: float | None = None) -> tuple[str, None]:
            seen.setdefault("timeouts", []).append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired("worker", timeout)
            return "", None

    process = TimedOutProcess()

    def _popen(*_args: Any, **kwargs: Any) -> TimedOutProcess:
        seen.update(kwargs)
        return process

    def _terminate(proc: object) -> None:
        assert proc is process
        seen["terminated"] = True
        process.returncode = -9

    monkeypatch.setattr(vcli.subprocess, "Popen", _popen)
    monkeypatch.setattr(vcli, "_terminate_worker_tree", _terminate)
    with pytest.raises(RuntimeError, match="timed out"):
        vcli.run_worker(audio, timeout_s=1.0)
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["timeouts"] == [1.0, None]
    assert seen["terminated"] is True
    if os.name == "nt":
        assert seen["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert seen["start_new_session"] is True


def test_track_claim_lease_and_old_owner_cannot_release_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired prior owner cannot clear the successor's immutable token."""
    from apps.vocals import cli as vcli

    clock = {"now": 100.0}
    monkeypatch.setattr(vcli.time, "time", lambda: clock["now"])
    cache_file = vcache.cache_path(tmp_path, "track")
    first = vcli._claim_track(cache_file, lease_s=10.0)
    assert first is not None
    first_record = vcli._read_claim_record(first.path)
    assert first_record is not None
    assert first_record["owner_token"] == first.owner_token
    assert first_record["heartbeat_at"] == 100.0
    assert first_record["lease_s"] == 10.0
    clock["now"] = 105.0
    assert vcli._claim_track(cache_file, lease_s=0.1) is None
    clock["now"] = 111.0
    successor = vcli._claim_track(cache_file, lease_s=20.0)
    assert successor is not None
    assert successor.owner_token != first.owner_token
    vcli._release_track_claim(first)
    record = vcli._read_claim_record(successor.path)
    assert record is not None
    assert record["owner_token"] == successor.owner_token
    assert record["released"] is False
    assert vcli._claim_track(cache_file, lease_s=0.1) is None
    vcli._release_track_claim(successor)


def test_normal_managed_claim_releases_and_allows_successor(tmp_path: Path) -> None:
    from apps.vocals import cli as vcli

    cache_file = vcache.cache_path(tmp_path, "normal-release")
    claim = vcli._claim_track(cache_file)
    assert claim is not None
    with vcli._managed_track_claim(claim):
        vcli._assert_track_claim(claim)
    record = vcli._read_claim_record(claim.path)
    assert record is not None
    assert record["released"] is True
    successor = vcli._claim_track(cache_file)
    assert successor is not None
    vcli._release_track_claim(successor)


@pytest.mark.parametrize("taskkill_unavailable", [False, True])
def test_windows_taskkill_failure_retains_nonexpiring_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    taskkill_unavailable: bool,
) -> None:
    """A dead leader does not prove its Windows descendant tree is gone."""
    from apps.vocals import cli as vcli

    class ExitedProcess:
        pid = 4242
        returncode = 0

    def _taskkill(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if taskkill_unavailable:
            raise FileNotFoundError("taskkill unavailable")
        return subprocess.CompletedProcess(
            args=["taskkill"],
            returncode=1,
            stdout="",
            stderr="access denied",
        )

    monkeypatch.setattr(vcli, "_WINDOWS", True)
    monkeypatch.setattr(vcli.subprocess, "run", _taskkill)
    cache_file = vcache.cache_path(tmp_path, "windows-cleanup")
    claim = vcli._claim_track(cache_file)
    assert claim is not None
    with pytest.raises(vcli.WorkerCleanupError):
        with vcli._managed_track_claim(claim):
            vcli._terminate_worker_tree(ExitedProcess())  # type: ignore[arg-type]
    record = vcli._read_claim_record(claim.path)
    assert record is not None
    assert record["released"] is False
    assert record["manual_recovery_required"] is True
    assert not claim.heartbeat_thread.is_alive()
    monkeypatch.setattr(
        vcli.time,
        "time",
        lambda: record["heartbeat_at"] + record["lease_s"] + 10_000,
    )
    assert vcli._claim_track(cache_file) is None


def test_posix_escalation_failure_retains_nonexpiring_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.vocals import cli as vcli

    class RunningProcess:
        # This test's premise is "SIGKILL was DENIED on a group that is still
        # live", and cleanup now tells that apart from "denied because the
        # group holds only corpses" -- an EPERM macOS returns for both. So the
        # pgid has to name a genuinely live group or the denial is correctly
        # read as benign and nothing raises. The runner's own group is the
        # cheapest live one; os.killpg is monkeypatched for the whole call
        # below, so no signal ever reaches it. It was a synthetic 4343, which
        # named no group at all.
        pid = os.getpgid(0)

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                raise subprocess.TimeoutExpired("worker", timeout)
            return 0

    def _kill_group(_pid: int, requested_signal: int) -> None:
        if requested_signal == signal.SIGKILL:
            raise PermissionError("forced escalation denial")

    monkeypatch.setattr(vcli, "_WINDOWS", False)
    monkeypatch.setattr(vcli.os, "killpg", _kill_group)
    cache_file = vcache.cache_path(tmp_path, "posix-cleanup")
    claim = vcli._claim_track(cache_file)
    assert claim is not None
    with pytest.raises(vcli.WorkerCleanupError):
        with vcli._managed_track_claim(claim):
            vcli._terminate_worker_tree(RunningProcess())  # type: ignore[arg-type]
    record = vcli._read_claim_record(claim.path)
    assert record is not None
    assert record["released"] is False
    assert record["manual_recovery_required"] is True
    assert not claim.heartbeat_thread.is_alive()
    monkeypatch.setattr(
        vcli.time,
        "time",
        lambda: record["heartbeat_at"] + record["lease_s"] + 10_000,
    )
    assert vcli._claim_track(cache_file) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_worker_timeout_reaps_descendant_before_claim_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out worker's TERM-resistant child is gone before unlock."""
    from apps.vocals import cli as vcli

    child_pid_file = tmp_path / "child.pid"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import signal, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)'])\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        vcli,
        "_worker_command",
        lambda _audio: [sys.executable, str(worker), str(child_pid_file)],
    )
    cache_file = vcache.cache_path(tmp_path, "process-tree")
    claim = vcli._claim_track(cache_file)
    assert claim is not None
    with vcli._managed_track_claim(claim):
        with pytest.raises(RuntimeError, match="timed out"):
            vcli.run_worker(child_pid_file, timeout_s=0.5)
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        vcli._assert_track_claim(claim)
    record = vcli._read_claim_record(claim.path)
    assert record is not None
    assert record["released"] is True


def test_pioneer_path_cannot_escape_share_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.shared import platform_paths
    from apps.vocals import cli as vcli

    root = tmp_path / "share"
    root.mkdir()
    monkeypatch.setattr(platform_paths, "SHARE_ROOT", root)
    with pytest.raises(ValueError, match="unsafe:share-path"):
        vcli._resolve_share_path("/PIONEER/../../outside.mp3")


# ----- live-data smoke (skips cleanly without local library data) ---------------------


def _real_data_dir() -> Path | None:
    override = os.environ.get("VOCALS_DATA_DIR")
    candidates = (
        [Path(override)]
        if override
        else [DATA_DIR, Path("/Users/user/Music/music-dj-tools/data")]
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
