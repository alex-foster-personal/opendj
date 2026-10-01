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
from typing import Any, Protocol, TypeVar

from apps.shared.paths import DATA_DIR
from apps.stems import cache_cli
from apps.stems.artifacts import (
    DEFAULT_STEMS_DIR,
    StemArtifactError,
    StemBundleNotFoundError,
    load_stem_bundle,
)
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


class _HasStableId(Protocol):
    @property
    def stable_id(self) -> str: ...


T = TypeVar("T", bound=_HasStableId)


def read_ids_file(path: Path) -> set[str]:
    """The stable ids a caller restricted this run to, one per line."""
    try:
        ids = set(path.read_text(encoding="utf-8").split())
    except OSError as error:
        raise SystemExit(f"error: cannot read --ids-file {path}: {error}") from error
    if not ids:
        raise SystemExit(f"error: --ids-file {path} lists no stable ids")
    return ids


def restrict_to_ids(todo: list[T], ids: set[str] | None) -> list[T]:
    """``todo`` in its own order, kept to ``ids`` when a caller gave any.

    This CLI only sees the local disk. A caller that knows more (the engine's
    refresh job knows which bundles R2 holds) names the tracks worth
    rendering; without a list the whole local todo stands.
    """
    if ids is None:
        return todo
    return [track for track in todo if track.stable_id in ids]


def cmd_trickle(args: argparse.Namespace) -> int:
    live = bool(getattr(args, "live", False))
    ctx = Ctx(data_dir=args.data_dir)
    root = stems_dir(ctx.data_dir)
    tracks = load_tracks(ctx, args.playlist)
    classify_stems(tracks, root)
    rank = best_playlist_rank(ctx, tracks)
    todo = order_todo([t for t in tracks if t.category == CATEGORY_TODO], rank)
    ids_file: Path | None = getattr(args, "ids_file", None)
    todo = restrict_to_ids(todo, None if ids_file is None else read_ids_file(ids_file))
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


def _default_tier() -> str:
    from apps.stems.tiers import DEFAULT_TIER

    return DEFAULT_TIER


_TIER_CHOICES = _tier_choices()


def resolve_audio_path(data_dir: Path, stable_id: str) -> Path:
    """The on-disk audio file for a stable_id, or raise.

    Public because the webui generate endpoint needs the SAME resolution the
    CLI uses; two resolvers would eventually disagree about which file a job
    ran on, and the manifest would not say which one was right.
    """
    import sqlite3

    from apps.shared.platform_paths import load_path_map
    from apps.shared.state import locations as state_locations

    db = Path(data_dir) / "state" / "state.db"
    path_map = load_path_map(Path(data_dir))
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT file_path FROM tracks "
            "WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"no track {stable_id!r} in {db}")
        canonical_path = row[0]
        path = state_locations.local_audio_path(
            conn, stable_id, path_map=path_map,
        )
    if path is not None and path.is_file():
        return path
    from apps.shared.crate_index import resolve_crate_audio

    path = resolve_crate_audio(stable_id)
    if path is not None and path.is_file():
        return path
    raise FileNotFoundError(
        f"track {stable_id!r} points at {canonical_path!r}, which is not a "
        "materialised file on this machine (no local track_locations row "
        "either). Relocate it before asking for stems."
    )


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


ENGINE_URL_ENV = "MDT_ENGINE_URL"
DEFAULT_ENGINE_URL = "http://127.0.0.1:9400"


def _engine_url() -> str:
    return os.environ.get(ENGINE_URL_ENV, DEFAULT_ENGINE_URL).rstrip("/")


def _post_json(path: str, body: dict[str, Any]) -> dict[str, Any]:
    import json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"{_engine_url()}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"engine POST {path} failed ({exc.code}): {detail}") from exc


def cmd_separate(args: argparse.Namespace) -> int:
    """Enqueue stems.separate via the engine jobs API (agent-native parity)."""
    from apps.stems.job import JOB_KIND

    payload: dict[str, Any] = {"tier": args.tier}
    if args.scope:
        payload["scope"] = args.scope
    elif args.stable_id:
        payload["stable_ids"] = list(args.stable_id)
    else:
        raise SystemExit("error: pass --stable-id (repeatable) or --scope pending")
    if args.data_dir is not None:
        payload["data_dir"] = str(args.data_dir)
    job = _post_json("/api/v1/jobs", {"kind": JOB_KIND, "payload": payload})
    if args.json:
        print(json.dumps(job, indent=2))
    else:
        print(f"enqueued job {job['id']} kind={job['kind']} status={job['status']}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    """Cancel a running stems job via POST /api/v1/jobs/{id}/cancel."""
    job = _post_json(f"/api/v1/jobs/{args.job_id}/cancel", {})
    if args.json:
        print(json.dumps(job, indent=2))
    else:
        print(f"cancelled job {job['id']} status={job['status']}")
    return 0


# ----- R2 hydration index + bulk hydrate (ADR-0024) --------------------------


def _default_journal_path(data_dir: Path) -> Path:
    """Same fixed path ``apps.stems.r2_migration`` journals to."""
    return Path(data_dir) / "state" / "stem-r2-migration.jsonl"


def cmd_build_index(args: argparse.Namespace) -> int:
    """Fold the R2 push journal into the local stem-index cache, mirrors
    ``POST /api/v1/stems/index/build``. ``--publish`` also overwrites the
    R2 pointer object so other machines can fetch it."""
    from apps.cloud import stem_index

    journal_path = args.journal or _default_journal_path(args.data_dir)
    index = stem_index.build_index_from_journal(journal_path)
    cache_path = stem_index.save_cached_index(args.data_dir, index)
    n_files = sum(len(files) for files in index.values())
    print(
        f"built index from {journal_path}: {len(index)} bundles, {n_files} files "
        f"-> cached at {cache_path}"
    )
    if not args.publish:
        return 0
    from apps.cloud.config import CloudConfig
    from apps.cloud.replicate import boto3_s3_client

    cfg = CloudConfig.from_env()
    etag = stem_index.publish_index(cfg, boto3_s3_client(cfg), index)
    print(f"published to r2://{cfg.audio_bucket}/{stem_index.INDEX_OBJECT_KEY} (etag {etag})")
    return 0


def cmd_bulk_hydrate(args: argparse.Namespace) -> int:
    """Hydrate many bundles from R2 within a byte budget, mirrors
    ``POST /api/v1/stems/bulk-hydrate``. Prints what it fetched and skipped,
    with reasons -- never silent."""
    from apps.cloud import stem_hydration, stem_index
    from apps.cloud.stem_source import resolve_stem_hydration_source

    if args.ids:
        stable_ids = [sid.strip() for sid in args.ids.split(",") if sid.strip()]
    elif args.playlist:
        ctx = Ctx(data_dir=args.data_dir)
        tracks = load_tracks(ctx, args.playlist)
        rank = best_playlist_rank(ctx, tracks)
        ordered = sorted(tracks, key=lambda t: rank.get(t.stable_id, 1 << 30))
        stable_ids = [t.stable_id for t in ordered]
    else:
        raise SystemExit("error: pass --ids a,b,c or --playlist NAME")

    source = resolve_stem_hydration_source(args.data_dir)
    if source is None:
        raise SystemExit(
            "error: bulk-hydrate needs cloud mode with R2 credentials or a configured hub"
        )
    from apps.cloud.stem_source import StemSourceError, hub_transport_failure_kind

    index = stem_index.load_cached_index(args.data_dir)
    if args.refresh_index or not index:
        try:
            source.refresh_index(args.data_dir, force=args.refresh_index or not index)
        except StemSourceError as exc:
            if hub_transport_failure_kind(exc) == "unreachable":
                raise SystemExit(f"error: SYNC_HUB_UNREACHABLE: {exc.message}") from exc
            raise SystemExit(f"error: {exc.code}: {exc.message}") from exc
        index = stem_index.load_cached_index(args.data_dir)
    if not index:
        raise SystemExit(
            "error: no stem bundle index published in R2 "
            "(run build-index --publish after the push rail has journaled bundles)"
        )
    try:
        report = stem_hydration.bulk_hydrate(
            stable_ids,
            data_dir=args.data_dir,
            source=source,
            index=index,
            byte_budget=args.budget_bytes,
            include_reserved=args.include_reserved,
            # Without this, hydrate_one falls back to the fixed DEFAULT_STEMS_DIR
            # regardless of --data-dir, which would write into the production
            # stems path from a run against a throwaway data dir.
            stems_dir=stems_dir(args.data_dir),
        )
    except StemSourceError as exc:
        if hub_transport_failure_kind(exc) == "unreachable":
            raise SystemExit(f"error: SYNC_HUB_UNREACHABLE: {exc.message}") from exc
        raise SystemExit(f"error: {exc.code}: {exc.message}") from exc
    if args.json:
        print(
            json.dumps(
                {
                    "fetched": [o.__dict__ for o in report.fetched],
                    "skipped": [o.__dict__ for o in report.skipped],
                    "bytes_fetched": report.bytes_fetched,
                },
                indent=2,
            )
        )
        return 0
    for outcome in report.fetched:
        print(f"  fetched {outcome.stable_id} ({outcome.status}, {outcome.bytes_fetched} bytes)")
    for outcome in report.skipped:
        print(f"  skipped {outcome.stable_id} ({outcome.reason})")
    print(
        f"done: {len(report.fetched)} fetched, {len(report.skipped)} skipped, "
        f"{report.bytes_fetched / 1e6:.1f} MB"
    )
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
    trickle.add_argument(
        "--ids-file", type=Path, default=None,
        help="render only the stable ids listed in this file, one per line",
    )
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

    sep = sub.add_parser(
        "separate",
        parents=[common],
        help="enqueue stems.separate on the engine (POST /api/v1/jobs)",
    )
    sep.add_argument("--stable-id", action="append", dest="stable_id", default=None)
    sep.add_argument("--scope", choices=("pending",), default=None)
    sep.add_argument("--tier", choices=_TIER_CHOICES, default=_default_tier())
    sep.add_argument("--json", action="store_true")
    sep.set_defaults(func=cmd_separate)

    cancel = sub.add_parser(
        "cancel",
        help="cancel a running engine job (POST /api/v1/jobs/{id}/cancel)",
    )
    cancel.add_argument("--job-id", required=True)
    cancel.add_argument("--json", action="store_true")
    cancel.set_defaults(func=cmd_cancel)

    build_index = sub.add_parser(
        "build-index", parents=[common],
        help="fold the R2 push journal into the local stem-index cache (ADR-0024)",
    )
    build_index.add_argument("--journal", type=Path, default=None)
    build_index.add_argument(
        "--publish", action="store_true",
        help="also overwrite the R2 pointer object so other machines can fetch it",
    )
    build_index.set_defaults(func=cmd_build_index)

    bulk_hydrate = sub.add_parser(
        "bulk-hydrate", parents=[common],
        help="hydrate many R2-indexed stem bundles within a byte budget (ADR-0024)",
    )
    bulk_ids = bulk_hydrate.add_mutually_exclusive_group(required=True)
    bulk_ids.add_argument("--ids", default=None, help="comma-separated stable_ids")
    bulk_ids.add_argument("--playlist", default=None)
    bulk_hydrate.add_argument("--budget-bytes", type=int, required=True)
    bulk_hydrate.add_argument(
        "--include-reserved", action="store_true",
        help="also hydrate stable_ids in the reservation guard file "
             "(state/stem-order-reserved-100.json) -- off by default",
    )
    bulk_hydrate.add_argument(
        "--refresh-index", action="store_true",
        help="fetch the published R2 index into the local cache before hydrating",
    )
    bulk_hydrate.add_argument("--json", action="store_true")
    bulk_hydrate.set_defaults(func=cmd_bulk_hydrate)

    cache_cli.register(sub)

    return p


def main(argv: list[str] | None = None) -> int:
    from apps.vocals.errors import UnknownPlaylistError

    try:
        args = build_parser().parse_args(argv)
        return int(args.func(args))
    except UnknownPlaylistError as exc:
        raise SystemExit(
            2,
            f"error: unknown playlist {exc.name!r}. "
            f"Known: {', '.join(exc.known) or '(none)'}",
        )


if __name__ == "__main__":
    raise SystemExit(main())
