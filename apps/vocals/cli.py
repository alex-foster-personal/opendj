"""``python -m apps.vocals`` - vocal-detection demucs gap-fill trickle CLI.

Fills the PVDI gap (61% of the library lacks rekordbox 7 vocal analysis,
SPIKE-B1) by trickling tracks through the standalone demucs worker
(``scripts/vocal_region_worker.py``, PEP 723, run via ``uv run`` so
torch/demucs never enter the repo venv) and writing
``data/state/vocal-cache/{stable_id}.json`` per the apps.vocals.cache
contract. The webui /anlz endpoint consults that cache when PVDI is
absent (status "demucs").

Requirements (mini-PRD):
  ✔︎ ✅ scan: coverage report pvdi / cached-demucs / missing-file / todo,
    with todo ETA at the SPIKE-B2 1.36x-realtime CPU rate; --playlist
    filter; --json machine-readable (agent-native parity).
    [if] a track has PVDI in its .2EX [then] it counts as pvdi
    [if] counts do not sum to total [then ⛔️]
    [if] --playlist names an unknown playlist [then ⛔️] explicit error
  ✔︎ ✅ trickle: process todo queue N tracks (--limit, default 5), ordered
    by (playlist with most on-disk members) desc then length asc; DRY-RUN
    by default, --live executes (mirrors apps.smartlists.refresh);
    idempotent via cache presence; missing files skipped EXPLICITLY with
    a log line; per-track wall time + running ETA on stdout.
    [if] run twice --live [then] second run recomputes nothing
    [if] a queued file vanished before its turn [then] "[SKIP]" line, no crash
    [if] --dry-run and --live both passed [then ⛔️] argparse rejects them
    [if] audio mtime changes during a worker run [then ⛔️] no cache write
  ✔︎ ✅ one: analyse a single --stable-id immediately (debug path), --force
    recomputes over a valid cache entry.
    [if] cache valid and no --force [then] no worker run, prints cached
  ✔︎ ✅ --data-dir overrides the repo-default data/ root everywhere
    (worktrees pass the primary checkout's data dir explicitly).
  → per-night budget (--max-minutes) - PARITY-TODO follow-up, not here.

Exact command lines:
  python -m apps.vocals scan --data-dir /Users/dev/Music/music-dj-tools/data
  python -m apps.vocals trickle --limit 1 --live \\
      --data-dir /Users/dev/Music/music-dj-tools/data

-Claude
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from apps.shared.paths import DATA_DIR
from apps.vocals import cache as vcache

# ----- CFG ---------------------------------------------------------------------
DEMUCS_REALTIME_FACTOR: float = 1.36   # SPIKE-B2 measured CPU rate (honest band 0.8-1.4x)
DEFAULT_TRICKLE_LIMIT: int = 5
SHARE_ROOT: Path = Path.home() / "Library" / "Pioneer" / "rekordbox" / "share"
STREAMING_PREFIXES: tuple[str, ...] = ("tidal:", "soundcloud:", "spotify:")
WORKER_SCRIPT: Path = Path(__file__).resolve().parents[2] / "scripts" / "vocal_region_worker.py"
_SQL_CHUNK: int = 500                  # keep IN (...) under SQLite's var cap

CATEGORY_PVDI = "pvdi"
CATEGORY_CACHED = "cached_demucs"
CATEGORY_MISSING = "missing_file"
CATEGORY_TODO = "todo"


@dataclass(frozen=True)
class Ctx:
    """Resolved data locations for one invocation (--data-dir aware)."""

    data_dir: Path

    @property
    def state_db(self) -> Path:
        return self.data_dir / "state" / "state.db"

    @property
    def master_db(self) -> Path:
        return self.data_dir / "master.plain.db"

    @property
    def cache_dir(self) -> Path:
        return vcache.cache_dir(self.data_dir)


@dataclass
class VocalTrack:
    """One rekordbox-mapped track with everything scan/trickle needs."""

    stable_id: str
    vendor_id: str
    title: str
    length_s: int
    folder_path: Optional[str]
    analysis_data_path: Optional[str]
    audio_path: Optional[Path]
    audio_on_disk: bool
    category: str = ""


# ----- db plumbing ---------------------------------------------------------------

def _open_ro(path: Path, label: str) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"{label} missing on disk: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _resolve_share_path(path: str) -> Path:
    """RECON-DATA.md section 1: /PIONEER/ paths are share-relative."""
    if path.startswith("/PIONEER/"):
        return SHARE_ROOT / path.lstrip("/")
    return Path(path)


def _chunks(seq: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ----- track loading -------------------------------------------------------------

def load_tracks(ctx: Ctx, playlist: Optional[str]) -> list[VocalTrack]:
    """All rekordbox-mapped tracks (optionally one playlist's members)."""
    state = _open_ro(ctx.state_db, "STATE_DB")
    try:
        if playlist is not None:
            pl_rows = state.execute(
                "SELECT playlist_id, name FROM playlists WHERE name = ?",
                (playlist,),
            ).fetchall()
            if not pl_rows:
                names = [
                    r[0] for r in state.execute(
                        "SELECT DISTINCT name FROM playlists ORDER BY name"
                    ).fetchall()
                ]
                raise SystemExit(
                    f"error: unknown playlist {playlist!r}. "
                    f"Known: {', '.join(names) or '(none)'}"
                )
            pl_ids = [r[0] for r in pl_rows]
            member_ids: set[str] = set()
            for chunk in _chunks(pl_ids, _SQL_CHUNK):
                marks = ",".join("?" * len(chunk))
                member_ids.update(
                    r[0] for r in state.execute(
                        "SELECT DISTINCT stable_id FROM playlist_memberships "
                        f"WHERE playlist_id IN ({marks})",
                        chunk,
                    ).fetchall()
                )
        mappings = state.execute(
            "SELECT t.stable_id, v.vendor_id, COALESCE(t.title, '') "
            "FROM tracks t "
            "JOIN track_vendor_ids v "
            "  ON v.stable_id = t.stable_id AND v.vendor = 'rekordbox'"
        ).fetchall()
    finally:
        state.close()

    if playlist is not None:
        mappings = [m for m in mappings if m[0] in member_ids]

    by_vendor: dict[str, tuple[str, str]] = {
        str(vid): (sid, title) for sid, vid, title in mappings
    }
    master = _open_ro(ctx.master_db, "MASTER_DB")
    rows: list[tuple[str, str, int, Optional[str], Optional[str]]] = []
    try:
        for chunk in _chunks(list(by_vendor), _SQL_CHUNK):
            marks = ",".join("?" * len(chunk))
            rows.extend(
                master.execute(
                    "SELECT ID, Title, Length, FolderPath, AnalysisDataPath "
                    "FROM djmdContent "
                    f"WHERE ID IN ({marks}) AND rb_local_deleted = 0",
                    chunk,
                ).fetchall()
            )
    finally:
        master.close()

    tracks: list[VocalTrack] = []
    for vendor_id, title, length_s, folder_path, adp in rows:
        stable_id, state_title = by_vendor[str(vendor_id)]
        audio: Optional[Path] = None
        on_disk = False
        if folder_path and not str(folder_path).startswith(STREAMING_PREFIXES):
            audio = _resolve_share_path(str(folder_path))
            on_disk = audio.is_file()
        tracks.append(VocalTrack(
            stable_id=stable_id,
            vendor_id=str(vendor_id),
            title=str(title or state_title),
            length_s=int(length_s) if length_s is not None else 0,
            folder_path=str(folder_path) if folder_path else None,
            analysis_data_path=str(adp) if adp else None,
            audio_path=audio,
            audio_on_disk=on_disk,
        ))
    tracks.sort(key=lambda t: t.stable_id)
    return tracks


# ----- classification --------------------------------------------------------------

def pvdi_present(path_2ex: Path) -> bool:
    """Cheap PVDI probe: seek-walk the PMAI section headers only (a scan
    over ~10k .2EX files must not read ~20 GB of full payloads)."""
    with path_2ex.open("rb") as fh:
        head = fh.read(12)
        if len(head) < 12 or head[:4] != b"PMAI":
            raise ValueError(f"not an ANLZ container: {path_2ex}")
        head_len, file_len = struct.unpack(">II", head[4:12])
        off = head_len
        while off + 12 <= file_len:
            fh.seek(off)
            section = fh.read(12)
            if len(section) < 12:
                break
            if section[:4] == b"PVDI":
                return True
            total_len = struct.unpack(">I", section[8:12])[0]
            if total_len <= 0:
                raise ValueError(f"corrupt section length in {path_2ex}")
            off += total_len
    return False


def classify(ctx: Ctx, tracks: list[VocalTrack]) -> None:
    """Assign each track exactly one category (pvdi wins over everything:
    an analyzed track is covered even if its audio has since moved)."""
    for tr in tracks:
        twoex: Optional[Path] = None
        if tr.analysis_data_path is not None:
            twoex = _resolve_share_path(tr.analysis_data_path).with_suffix(".2EX")
        if twoex is not None and twoex.is_file() and pvdi_present(twoex):
            tr.category = CATEGORY_PVDI
            continue
        entry = vcache.load_valid_entry(
            vcache.cache_path(ctx.data_dir, tr.stable_id), tr.audio_path
        )
        if entry is not None:
            tr.category = CATEGORY_CACHED
            continue
        if not tr.audio_on_disk:
            tr.category = CATEGORY_MISSING
            continue
        tr.category = CATEGORY_TODO


# ----- trickle queue ordering -------------------------------------------------------

def best_playlist_rank(ctx: Ctx, tracks: list[VocalTrack]) -> dict[str, tuple[int, str]]:
    """stable_id -> (largest on-disk member count over its playlists, that
    playlist's name). Tracks in no playlist rank (0, '')."""
    on_disk_ids = {t.stable_id for t in tracks if t.audio_on_disk}
    state = _open_ro(ctx.state_db, "STATE_DB")
    try:
        rows = state.execute(
            "SELECT m.playlist_id, p.name, m.stable_id "
            "FROM playlist_memberships m "
            "JOIN playlists p ON p.playlist_id = m.playlist_id"
        ).fetchall()
    finally:
        state.close()

    members: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for pl_id, name, sid in rows:
        members.setdefault(pl_id, []).append(sid)
        names[pl_id] = name
    counts = {
        pl_id: sum(1 for sid in sids if sid in on_disk_ids)
        for pl_id, sids in members.items()
    }

    rank: dict[str, tuple[int, str]] = {}
    for pl_id, sids in members.items():
        for sid in sids:
            if counts[pl_id] > rank.get(sid, (0, ""))[0]:
                rank[sid] = (counts[pl_id], names[pl_id])
    return rank


def order_todo(
    todo: list[VocalTrack], rank: dict[str, tuple[int, str]]
) -> list[VocalTrack]:
    """Priority: playlists with most on-disk members first, then shortest
    first (fast wins early), then stable_id for determinism."""
    return sorted(
        todo,
        key=lambda t: (-rank.get(t.stable_id, (0, ""))[0], t.length_s, t.stable_id),
    )


# ----- worker invocation --------------------------------------------------------------

def run_worker(audio_path: Path) -> dict[str, Any]:
    """Run the PEP 723 demucs worker via ``uv run`` and parse its stdout
    JSON. Worker logs pass through on stderr; a non-zero exit raises."""
    cmd = ["uv", "run", "--script", str(WORKER_SCRIPT), str(audio_path)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"vocal_region_worker failed (exit {proc.returncode}) "
            f"for {audio_path}"
        )
    return json.loads(proc.stdout)


# ----- output helpers ------------------------------------------------------------------

def _fmt_dur(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    elif seconds >= 60:
        return f"{seconds / 60:.1f}m"
    else:
        return f"{seconds:.0f}s"


def _counts(tracks: list[VocalTrack]) -> dict[str, int]:
    counts = {
        CATEGORY_PVDI: 0, CATEGORY_CACHED: 0,
        CATEGORY_MISSING: 0, CATEGORY_TODO: 0,
    }
    for tr in tracks:
        counts[tr.category] += 1
    return counts


# ----- subcommands ----------------------------------------------------------------------

def cmd_scan(args: argparse.Namespace) -> int:
    ctx = Ctx(data_dir=args.data_dir)
    tracks = load_tracks(ctx, args.playlist)
    classify(ctx, tracks)
    counts = _counts(tracks)
    if sum(counts.values()) != len(tracks):
        raise AssertionError("category counts do not sum to total - bug")
    todo_audio_s = sum(t.length_s for t in tracks if t.category == CATEGORY_TODO)
    eta_s = todo_audio_s * DEMUCS_REALTIME_FACTOR

    if args.json:
        print(json.dumps({
            "data_dir": str(ctx.data_dir),
            "playlist": args.playlist,
            "total": len(tracks),
            **counts,
            "todo_audio_s": todo_audio_s,
            "todo_eta_s": round(eta_s, 1),
            "realtime_factor": DEMUCS_REALTIME_FACTOR,
        }, indent=1))
        return 0

    scope = f" playlist={args.playlist!r}" if args.playlist else ""
    print(f"vocal coverage scan{scope} (data-dir {ctx.data_dir})")
    print(f"  pvdi (rekordbox):  {counts[CATEGORY_PVDI]:>6}")
    print(f"  cached-demucs:     {counts[CATEGORY_CACHED]:>6}")
    print(f"  missing-file:      {counts[CATEGORY_MISSING]:>6}")
    print(
        f"  todo:              {counts[CATEGORY_TODO]:>6}"
        f"  ({_fmt_dur(todo_audio_s)} audio, "
        f"ETA {_fmt_dur(eta_s)} at {DEMUCS_REALTIME_FACTOR}x realtime)"
    )
    print(f"  total:             {len(tracks):>6}")
    return 0


def _process_one(
    ctx: Ctx, tr: VocalTrack, prefix: str
) -> tuple[float, dict[str, Any]]:
    """Run the worker for one track and write its cache entry.

    The recorded ``audio_mtime`` is captured BEFORE the (multi-minute)
    worker run: if the file is replaced mid-analysis the regions belong
    to the old content, so recording the pre-run mtime guarantees the
    entry self-invalidates against the new file instead of poisoning
    the cache. A detected change also fails loudly right here.
    """
    assert tr.audio_path is not None
    pre_mtime = tr.audio_path.stat().st_mtime
    t0 = time.perf_counter()
    result = run_worker(tr.audio_path)
    post_mtime = tr.audio_path.stat().st_mtime
    if post_mtime != pre_mtime:
        raise RuntimeError(
            f"audio file changed during analysis: {tr.audio_path} "
            f"(mtime {pre_mtime} -> {post_mtime}); regions were computed "
            f"from the old content, refusing to cache them"
        )
    entry = vcache.write_entry(
        vcache.cache_path(ctx.data_dir, tr.stable_id), result, tr.audio_path,
        audio_mtime=pre_mtime,
    )
    wall_s = time.perf_counter() - t0
    rate = wall_s / tr.length_s if tr.length_s else float("nan")
    print(
        f"{prefix} done in {wall_s:.1f}s ({rate:.2f}x realtime) "
        f"regions={len(entry['regions'])} cov={entry['coverage_pct']}% "
        f"-> {vcache.cache_path(ctx.data_dir, tr.stable_id)}"
    )
    return wall_s, entry


def cmd_trickle(args: argparse.Namespace) -> int:
    ctx = Ctx(data_dir=args.data_dir)
    live = bool(args.live)
    tracks = load_tracks(ctx, args.playlist)
    classify(ctx, tracks)

    # Explicit, never silent: every missing-file track is logged (stderr -
    # stdout carries the plan / progress lines).
    n_missing = 0
    for tr in tracks:
        if tr.category == CATEGORY_MISSING:
            n_missing += 1
            print(f"[SKIP missing-file] {tr.stable_id} {tr.title!r}: "
                  f"{tr.folder_path or '(no FolderPath)'}", file=sys.stderr)
    if n_missing:
        print(f"[skipped {n_missing} missing-file tracks; see stderr]")

    rank = best_playlist_rank(ctx, tracks)
    todo = order_todo(
        [t for t in tracks if t.category == CATEGORY_TODO], rank
    )
    batch = todo[:args.limit]
    batch_audio_s = sum(t.length_s for t in batch)
    est_s = batch_audio_s * DEMUCS_REALTIME_FACTOR

    mode = "live" if live else "dry-run"
    print(
        f"[{mode}] {len(batch)} of {len(todo)} todo tracks selected "
        f"(--limit {args.limit}); est {_fmt_dur(est_s)} "
        f"at {DEMUCS_REALTIME_FACTOR}x realtime"
    )
    for i, tr in enumerate(batch, 1):
        count, name = rank.get(tr.stable_id, (0, ""))
        pl = f"playlist={name!r}({count} on disk)" if count else "no playlist"
        print(
            f"  {i}. {tr.stable_id} {tr.title!r} {tr.length_s}s {pl} "
            f"est {_fmt_dur(tr.length_s * DEMUCS_REALTIME_FACTOR)}"
        )
    if not live:
        print("[dry-run] no work performed; pass --live to execute")
        return 0

    done_audio_s = 0.0
    done_wall_s = 0.0
    completed = 0
    for i, tr in enumerate(batch, 1):
        remaining_audio_s = sum(t.length_s for t in batch[i:])
        if tr.audio_path is None or not tr.audio_path.is_file():
            print(f"[SKIP vanished] {tr.stable_id} {tr.title!r}: {tr.audio_path}")
            continue
        if vcache.load_valid_entry(
            vcache.cache_path(ctx.data_dir, tr.stable_id), tr.audio_path
        ) is not None:
            print(f"[SKIP cached] {tr.stable_id} {tr.title!r}: valid cache entry")
            continue
        print(f"[{i}/{len(batch)}] {tr.stable_id} {tr.title!r} ({tr.length_s}s)")
        wall_s, _entry = _process_one(ctx, tr, f"[{i}/{len(batch)}]")
        completed += 1
        done_audio_s += tr.length_s
        done_wall_s += wall_s
        live_rate = done_wall_s / done_audio_s if done_audio_s else 0.0
        print(
            f"        running: {completed} done, {len(batch) - i} left, "
            f"est remaining {_fmt_dur(remaining_audio_s * live_rate)} "
            f"(measured {live_rate:.2f}x realtime)"
        )
    print(f"[live] finished: {completed} analysed, "
          f"{len(batch) - completed} skipped, wall {_fmt_dur(done_wall_s)}")
    return 0


def cmd_one(args: argparse.Namespace) -> int:
    ctx = Ctx(data_dir=args.data_dir)
    tracks = [
        t for t in load_tracks(ctx, None) if t.stable_id == args.stable_id
    ]
    if not tracks:
        raise SystemExit(
            f"error: stable_id {args.stable_id!r} has no rekordbox mapping "
            f"in {ctx.state_db}"
        )
    tr = tracks[0]
    if tr.audio_path is None or not tr.audio_path.is_file():
        raise SystemExit(
            f"error: audio file missing for {tr.stable_id}: "
            f"{tr.folder_path or '(no FolderPath)'}"
        )
    cache_file = vcache.cache_path(ctx.data_dir, tr.stable_id)
    existing = vcache.load_valid_entry(cache_file, tr.audio_path)
    if existing is not None and not args.force:
        print(f"[cached] {tr.stable_id} {tr.title!r}: valid entry at "
              f"{cache_file} ({len(existing['regions'])} regions, "
              f"cov={existing['coverage_pct']}%); use --force to recompute")
        return 0
    print(f"[one] {tr.stable_id} {tr.title!r} ({tr.length_s}s) "
          f"est {_fmt_dur(tr.length_s * DEMUCS_REALTIME_FACTOR)}")
    _process_one(ctx, tr, "[one]")
    return 0


# ----- parser ----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.vocals",
        description="demucs vocal-region gap-fill: scan coverage, trickle "
                    "the todo queue, or analyse one track.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir", type=Path, default=DATA_DIR,
        help=f"data root holding state/state.db, master.plain.db and "
             f"state/vocal-cache (default: {DATA_DIR})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser(
        "scan", parents=[common], help="report vocal coverage + trickle ETA"
    )
    scan.add_argument("--playlist", default=None, help="limit to one playlist name")
    scan.add_argument("--json", action="store_true", help="machine-readable output")
    scan.set_defaults(func=cmd_scan)

    trickle = sub.add_parser(
        "trickle", parents=[common],
        help="process the todo queue (dry-run unless --live)",
    )
    trickle.add_argument("--limit", type=int, default=DEFAULT_TRICKLE_LIMIT,
                         help=f"max tracks this run (default {DEFAULT_TRICKLE_LIMIT})")
    trickle.add_argument("--playlist", default=None,
                         help="limit the queue to one playlist name")
    mode = trickle.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="plan only, no worker runs (the default)")
    mode.add_argument("--live", action="store_true",
                      help="actually run the demucs worker + write cache")
    trickle.set_defaults(func=cmd_trickle)

    one = sub.add_parser(
        "one", parents=[common], help="analyse a single track (debug path)"
    )
    one.add_argument("--stable-id", required=True)
    one.add_argument("--force", action="store_true",
                     help="recompute even when a valid cache entry exists")
    one.set_defaults(func=cmd_one)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
