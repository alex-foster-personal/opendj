"""``python -m apps.stems`` - htdemucs 4-stem bundle trickle CLI.

Writes ``data/state/stems/<stable_id>/`` bundles for the webui mute/solo
path (PR #228). Reuses track loading from ``apps.vocals.cli``. Torch stays
out of the repo venv via ``uv run scripts/stem_bundle_worker.py``.

  python -m apps.stems scan --playlist "Once Loft Twice Found"
  python -m apps.stems trickle --playlist "Once Loft Twice Found" --limit 1 --live
  python -m apps.stems one --stable-id ID --live

-Claude
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from apps.shared.paths import DATA_DIR
from apps.vocals.cli import (
    CATEGORY_MISSING,
    CATEGORY_TODO,
    Ctx,
    VocalTrack,
    _fmt_dur,
    best_playlist_rank,
    load_tracks,
    order_todo,
)
from apps.webui.server.stem_artifacts import (
    DEFAULT_STEMS_DIR,
    StemArtifactError,
    StemBundleNotFoundError,
    load_stem_bundle,
)

# ----- CFG -------------------------------------------------------------------
DEMUCS_REALTIME_FACTOR = 1.36
DEFAULT_TRICKLE_LIMIT = 5
WORKER_TIMEOUT_S = 60 * 60
WORKER_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "stem_bundle_worker.py"
)
CATEGORY_CACHED = "cached_stems"
CATEGORY_MISSING_STEM = CATEGORY_MISSING  # reuse vocals missing-file label


# ----- classify --------------------------------------------------------------


def stems_dir(data_dir: Path) -> Path:
    if data_dir.resolve() == DATA_DIR.resolve():
        return DEFAULT_STEMS_DIR
    return data_dir / "state" / "stems"


def has_valid_bundle(stable_id: str, root: Path) -> bool:
    try:
        load_stem_bundle(stable_id, stems_dir=root)
        return True
    except StemBundleNotFoundError:
        return False
    except StemArtifactError:
        return False


def classify_stems(tracks: list[VocalTrack], root: Path) -> None:
    for tr in tracks:
        if not tr.audio_on_disk or tr.audio_path is None:
            tr.category = CATEGORY_MISSING
            continue
        if has_valid_bundle(tr.stable_id, root):
            tr.category = CATEGORY_CACHED
        else:
            tr.category = CATEGORY_TODO


def _counts(tracks: list[VocalTrack]) -> dict[str, int]:
    out = {CATEGORY_CACHED: 0, CATEGORY_MISSING: 0, CATEGORY_TODO: 0}
    for tr in tracks:
        out[tr.category] = out.get(tr.category, 0) + 1
    return out


# ----- worker ----------------------------------------------------------------


def run_worker(
    *,
    audio: Path,
    stable_id: str,
    out_dir: Path,
    device: str,
    timeout_s: float = WORKER_TIMEOUT_S,
) -> dict[str, Any]:
    py = os.environ.get("MDT_STEM_WORKER_PYTHON") or os.environ.get(
        "MDT_VOCAL_WORKER_PYTHON"
    )
    if py:
        cmd = [
            py,
            str(WORKER_SCRIPT),
            "--audio",
            str(audio),
            "--stable-id",
            stable_id,
            "--out-dir",
            str(out_dir),
            "--device",
            device,
        ]
    else:
        cmd = [
            "uv",
            "run",
            "--no-sync",
            str(WORKER_SCRIPT),
            "--audio",
            str(audio),
            "--stable-id",
            stable_id,
            "--out-dir",
            str(out_dir),
            "--device",
            device,
        ]
    env = os.environ.copy()
    env["MDT_STEM_WORKER_DEVICE"] = device
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"stem worker failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[-2000:]}"
        )
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("stem worker produced no stdout JSON")
    return json.loads(lines[-1])


# ----- commands --------------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> int:
    ctx = Ctx(data_dir=args.data_dir)
    root = stems_dir(ctx.data_dir)
    tracks = load_tracks(ctx, args.playlist)
    classify_stems(tracks, root)
    counts = _counts(tracks)
    todo_audio_s = sum(t.length_s for t in tracks if t.category == CATEGORY_TODO)
    eta_s = todo_audio_s * DEMUCS_REALTIME_FACTOR
    if args.json:
        print(
            json.dumps(
                {
                    "data_dir": str(ctx.data_dir),
                    "stems_dir": str(root),
                    "playlist": args.playlist,
                    "total": len(tracks),
                    **counts,
                    "todo_audio_s": todo_audio_s,
                    "todo_eta_s": round(eta_s, 1),
                    "realtime_factor": DEMUCS_REALTIME_FACTOR,
                },
                indent=1,
            )
        )
        return 0
    scope = f" playlist={args.playlist!r}" if args.playlist else ""
    print(f"stem coverage scan{scope} (data-dir {ctx.data_dir})")
    print(f"  cached-stems:      {counts[CATEGORY_CACHED]:>6}")
    print(f"  missing-file:      {counts[CATEGORY_MISSING]:>6}")
    print(
        f"  todo:              {counts[CATEGORY_TODO]:>6}"
        f"  ({_fmt_dur(todo_audio_s)} audio, "
        f"ETA {_fmt_dur(eta_s)} at {DEMUCS_REALTIME_FACTOR}x realtime)"
    )
    print(f"  total:             {len(tracks):>6}")
    print(f"  stems_dir:         {root}")
    return 0


def cmd_trickle(args: argparse.Namespace) -> int:
    live = bool(getattr(args, "live", False))
    ctx = Ctx(data_dir=args.data_dir)
    root = stems_dir(ctx.data_dir)
    tracks = load_tracks(ctx, args.playlist)
    classify_stems(tracks, root)
    rank = best_playlist_rank(ctx, tracks)
    todo = order_todo([t for t in tracks if t.category == CATEGORY_TODO], rank)
    batch = todo[: args.limit]
    device = (
        args.device
        or os.environ.get("MDT_STEM_WORKER_DEVICE")
        or os.environ.get("MDT_VOCAL_WORKER_DEVICE")
        or "auto"
    )
    print(
        f"stem trickle {'LIVE' if live else 'DRY-RUN'} "
        f"n={len(batch)}/{len(todo)} device={device}"
    )
    if not batch:
        print("nothing to do")
        return 0
    ok = 0
    for i, tr in enumerate(batch, 1):
        assert tr.audio_path is not None
        out = root / tr.stable_id
        print(f"[{i}/{len(batch)}] {tr.stable_id} {tr.title!r} -> {out}")
        if not live:
            continue
        t0 = time.perf_counter()
        try:
            result = run_worker(
                audio=tr.audio_path,
                stable_id=tr.stable_id,
                out_dir=out,
                device=device,
            )
        except Exception as exc:
            print(f"  FAIL: {exc}", file=sys.stderr)
            continue
        wall = time.perf_counter() - t0
        print(
            f"  OK wall={wall:.1f}s rt={result.get('realtime_factor')} "
            f"device={result.get('device_used')}"
        )
        ok += 1
    print(f"done: {ok}/{len(batch)} ok" if live else f"dry-run listed {len(batch)}")
    return 0 if (not live) or ok > 0 or not batch else 1


def cmd_one(args: argparse.Namespace) -> int:
    if not args.live:
        raise SystemExit("error: `one` requires --live (refuses dry-run)")
    ctx = Ctx(data_dir=args.data_dir)
    root = stems_dir(ctx.data_dir)
    tracks = load_tracks(ctx, None)
    match = next((t for t in tracks if t.stable_id == args.stable_id), None)
    if match is None:
        raise SystemExit(f"error: unknown stable_id {args.stable_id!r}")
    if not match.audio_on_disk or match.audio_path is None:
        raise SystemExit(f"error: audio missing for {args.stable_id}")
    out = root / match.stable_id
    if has_valid_bundle(match.stable_id, root) and not args.force:
        print(f"already have valid bundle at {out} (pass --force to recompute)")
        return 0
    device = (
        args.device
        or os.environ.get("MDT_STEM_WORKER_DEVICE")
        or os.environ.get("MDT_VOCAL_WORKER_DEVICE")
        or "auto"
    )
    print(f"stem one LIVE {match.stable_id} {match.title!r} device={device}")
    result = run_worker(
        audio=match.audio_path,
        stable_id=match.stable_id,
        out_dir=out,
        device=device,
    )
    print(json.dumps(result, indent=2))
    return 0


# Derived, never typed. A hardcoded ("S","M","L") here was a FOURTH mirror of
# the tier table, and it silently disagreed with the API: `estimate --tier
# LOCAL` was rejected while GET /stems/estimate happily returned a LOCAL row.
def _tier_choices() -> tuple[str, ...]:
    from apps.stems.tiers import TIER_ORDER

    return TIER_ORDER


_TIER_CHOICES = _tier_choices()


def resolve_audio_path(data_dir: Path, stable_id: str) -> Path:
    """The on-disk audio file for a stable_id, or raise.

    Public because the webui generate endpoint needs the SAME resolution the
    CLI uses; two resolvers would eventually disagree about which file a job
    ran on, and the manifest would not say which one was right.
    """
    import sqlite3

    db = Path(data_dir) / "state" / "state.db"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT file_path FROM tracks "
            "WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()
    if row is None:
        raise FileNotFoundError(f"no track {stable_id!r} in {db}")
    if not row[0]:
        raise FileNotFoundError(f"track {stable_id!r} has no file_path in {db}")
    from apps.shared.platform_paths import load_path_map, resolve_asset_path

    mapped = resolve_asset_path(
        str(row[0]), path_map=load_path_map(Path(data_dir))
    )
    path = mapped.resolved
    if path is None or not path.is_file():
        from apps.shared.crate_index import resolve_crate_audio

        path = resolve_crate_audio(stable_id)
    if path is None or not path.is_file():
        raise FileNotFoundError(
            f"track {stable_id!r} points at {row[0]}, which resolves as "
            f"{path} ({mapped.reason}) and is not a materialised file. "
            "Relocate it before asking for stems."
        )
    return path


def _duration_from_state(data_dir: Path, stable_id: str) -> float:
    """Track length in seconds from state.db. Raises if the row has none."""
    import sqlite3

    db = data_dir / "state" / "state.db"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT duration_ms FROM tracks "
            "WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()
    if row is None:
        raise SystemExit(f"error: no track {stable_id!r} in {db}")
    if not row[0]:
        raise SystemExit(
            f"error: track {stable_id!r} has no duration in state.db, so the "
            "estimate would be invented. Pass --seconds explicitly."
        )
    return float(row[0]) / 1000.0


def cmd_estimate(args: argparse.Namespace) -> int:
    """Per-tier wall clock and GPU cost. Mirrors GET /api/v1/stems/estimate.

    Refuses to answer for an unbenchmarked (tier, card) pair rather than
    interpolating -- see apps/stems/tiers.py.
    """
    from apps.stems import tiers as tiercfg

    data_dir = getattr(args, "data_dir", None) or DATA_DIR
    tiercfg.load_measured_throughput(Path(data_dir).parent)

    if args.seconds is None and args.stable_id is None:
        raise SystemExit("error: pass --seconds or --stable-id")
    duration_s = (
        args.seconds if args.seconds is not None
        else _duration_from_state(Path(data_dir), args.stable_id)
    )

    keys = [args.tier] if args.tier else list(tiercfg.TIERS)
    rows: list[dict[str, Any]] = []
    for key in keys:
        tier = tiercfg.get_tier(key)
        card = args.gpu or tier.gpu
        row: dict[str, Any] = {
            "tier": tier.key, "name": tier.name, "gpu": card,
            "preset": tier.preset_tag,
        }
        try:
            row["seconds"] = round(tiercfg.estimate_seconds(duration_s, key, card), 1)
            row["usd"] = round(tiercfg.estimate_usd(duration_s, key, card), 4)
            row["measured"] = True
        except tiercfg.ThroughputNotMeasured as exc:
            row["measured"] = False
            row["unavailable_reason"] = str(exc)
        rows.append(row)

    if args.json:
        print(json.dumps({"duration_s": duration_s, "tiers": rows}, indent=2))
        return 0

    print(f"track {_fmt_dur(duration_s)} ({duration_s:.0f}s)")
    print(f"{'tier':<6} {'name':<11} {'gpu':<10} {'wall':>13} {'usd':>10}  preset")
    print(f"{'-' * 6} {'-' * 11} {'-' * 10} {'-' * 13} {'-' * 10}  {'-' * 22}")
    for row in rows:
        if row["measured"]:
            wall = f"{row['seconds']:.1f}s"
            usd = f"${row['usd']:.4f}"
        else:
            wall = usd = "NOT MEASURED"
        print(
            f"{row['tier']:<6} {row['name']:<11} {row['gpu']:<10} "
            f"{wall:>13} {usd:>10}  {row['preset']}"
        )
    unmeasured = [r for r in rows if not r["measured"]]
    if unmeasured:
        print(f"\n{len(unmeasured)} rung(s) have no benchmark on this card:")
        for row in unmeasured:
            print(f"  {row['tier']}: {row['unavailable_reason']}")
    # Non-zero ONLY when the caller asked for one specific rung and that rung
    # cannot be answered. Listing the whole ladder is informational, so failing
    # it would make the common call look broken and train people to ignore it.
    if args.tier and unmeasured:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m apps.stems")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", type=Path, default=DATA_DIR)
    common.add_argument(
        "--device",
        default=None,
        choices=("auto", "cpu", "cuda", "mps"),
        help="worker device (default auto / env)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", parents=[common])
    scan.add_argument("--playlist", default=None)
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=cmd_scan)

    trickle = sub.add_parser("trickle", parents=[common])
    trickle.add_argument("--playlist", default=None)
    trickle.add_argument("--limit", type=int, default=DEFAULT_TRICKLE_LIMIT)
    # A mutating subcommand must state its mode explicitly. A bare --live with
    # a silent dry-run default reads as "safe by default" while leaving the
    # operator no way to say which they meant, so argparse refuses instead.
    trickle_mode = trickle.add_mutually_exclusive_group(required=True)
    trickle_mode.add_argument(
        "--dry-run", action="store_true", help="list the batch only, run no worker"
    )
    trickle_mode.add_argument(
        "--live", action="store_true", help="execute the worker for each queued track"
    )
    trickle.set_defaults(func=cmd_trickle)

    one = sub.add_parser("one", parents=[common])
    one.add_argument("--stable-id", required=True)
    one.add_argument("--live", action="store_true", required=True)
    one.add_argument("--force", action="store_true")
    one.set_defaults(func=cmd_one)

    est = sub.add_parser(
        "estimate", parents=[common],
        help="how long each S/M/L tier takes for a track, on Modal",
    )
    est.add_argument(
        "--seconds", type=float, default=None,
        help="track duration; or use --stable-id to read it from state.db",
    )
    est.add_argument("--stable-id", default=None)
    est.add_argument("--tier", choices=_TIER_CHOICES, default=None,
                     help="default: show every rung of the ladder")
    est.add_argument("--gpu", default=None, help="override the card")
    est.add_argument("--json", action="store_true")
    est.set_defaults(func=cmd_estimate)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
