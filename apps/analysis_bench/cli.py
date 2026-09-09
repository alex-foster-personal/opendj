"""`python -m apps.analysis_bench` -- the one command four lanes drive.

    lanes                                   what is registered, and what refuses
    fixtures seal   --lane --version --dir  turn a staged directory into a bundle
    fixtures verify --lane --version        prove a local bundle is what it claims
    fixtures build  --lane                  route to the lane's fixture builder
    fixtures push   --lane --version        upload a verified bundle to the store
    fixtures pull   --lane --version        download, verify, refuse on mismatch
    score --lane --bundle-dir --arm n=f     score arms already run
    run   --lane --candidate [--post]       run candidate plus controls, score, log

WHY `run` RUNS THE CONTROLS TOO unless told not to. A round without a floor and
a ceiling cannot be read, and `rounds.append_round` refuses one, so making the
controls opt-out rather than opt-in is what keeps the common path honest.

WHY `--post` IS NOT THE DEFAULT. Appending to a spec is a commit-shaped act.
Scoring is cheap and repeatable; the log entry is the thing a later session
trusts, so it is asked for explicitly.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from apps.analysis_bench import bundles, lanes, rounds, stores
from apps.analysis_bench.scorers import get_scorer

BUNDLE_ROOT = Path("data/bench")


def _bundle_dir(lane: str, version: str, override: str | None) -> Path:
    return Path(override) if override else BUNDLE_ROOT / lane / version


def _host() -> str:
    return f"{platform.node()} ({platform.system().lower()} {platform.machine()})"


def _cmd_lanes(_args: argparse.Namespace) -> int:
    for name, lane in sorted(lanes.LANES.items()):
        scorer = lane.scorer_module or "NO SCORER YET (follow-up slice of #1477)"
        print(f"{name}: scorer {scorer}")
        print(f"  truth      {lane.truth}")
        print(f"  log        {lane.log_path} (next round >= {lane.round_floor})")
        print(f"  builder    {lane.fixture_builder}")
        for candidate in lane.candidates:
            print(f"  {candidate.role:<17} {candidate.name}: {candidate.note}")
    return 0


def _cmd_fixtures(args: argparse.Namespace) -> int:
    lane = lanes.get_lane(args.lane)
    if args.action == "build":
        raise SystemExit(
            f"[bench] this harness does not build {lane.name} fixtures itself; its builder is:\n"
            f"    {lane.fixture_builder}\n"
            "Build there, `fixtures seal` the staged directory, then `fixtures push` it."
        )

    if args.action == "seal":
        staged = Path(args.dir)
        manifest = bundles.seal_bundle(staged, lane=lane.name, version=args.version)
        print(f"[bench] sealed {staged}: {manifest['payload_count']} payload files, "
              f"{manifest['payload_bytes']} bytes")
        print(f"[bench] bundle_id {manifest['bundle_id']}")
        return 0

    bundle = _bundle_dir(lane.name, args.version, args.bundle_dir)
    if args.action == "verify":
        manifest = bundles.verify_bundle(bundle)
        print(f"[bench] {bundle} verifies: bundle_id {manifest['bundle_id']}")
        return 0
    if args.action == "push":
        store = stores.resolve_store(args.store)
        where = stores.push_bundle(bundle, store, lane=lane.name, version=args.version)
        print(f"[bench] pushed {bundle} -> {where}")
        return 0
    store = stores.resolve_store(args.store)
    dest = Path(args.dest) if args.dest else bundle
    pulled = stores.pull_bundle(
        store, lane=lane.name, version=args.version, dest=dest,
        expect_bundle_id=args.expect_bundle_id,
    )
    print(f"[bench] pulled {store.describe()} -> {pulled}, checksums verified")
    return 0


# The harness stamps this into every arm it produces, because the harness is
# what pointed the candidate at a bundle and is therefore the only party that
# can testify to it. `score` then requires it to match.
ARM_BUNDLE_KEY = "bundle_id"


def _stamp_arm(path: Path, bundle_id: str) -> dict[str, Any]:
    """Record which bundle this arm was measured on, in the arm's own file."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[ARM_BUNDLE_KEY] = bundle_id
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return payload


def _load_arm(lane: lanes.Lane, spec: str, bundle_id: str) -> tuple[str, dict[str, Any]]:
    """Read one arm's results, refusing any that was not measured on this bundle.

    TWO CLAIMS ARE CHECKED, because the label carries both. The file must name
    the candidate the label names -- `--arm beat_this=constant_128.json` was
    accepted and printed the control's numbers under Beat This -- and it must
    carry this bundle's stamp.

    A rebuild that keeps the stable ids but changes the audio or the scoring
    windows produces results that still LOOK loadable, and the report would then
    stamp them with the new bundle's id -- attributing one bundle's numbers to
    another (Codex P1 BLOCKING, PR #1582). An unstamped file is refused for the
    same reason: absence of the claim is not evidence for it.
    """
    name, _, path = spec.partition("=")
    if not path:
        raise SystemExit(f"[bench] --arm wants name=path/to/results.json, got {spec!r}")
    candidate = lanes.get_candidate(lane, name)
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    emitted = payload.get("candidate")
    if emitted != name:
        raise SystemExit(
            f"[bench] --arm {name}={path} holds results emitted by {emitted!r}, not {name!r}. "
            "The label decides the row heading, the role and the note the table carries, so "
            "accepting this would present one candidate's measurements as another's."
        )
    stamped = payload.get(ARM_BUNDLE_KEY)
    if stamped != bundle_id:
        raise SystemExit(
            f"[bench] arm {name} ({path}) carries bundle_id {stamped!r}, but this bundle is "
            f"{bundle_id}. Re-run it against this bundle with `run`, which stamps the arm it "
            "produces. Scoring it here would attribute another bundle's numbers to this one."
        )
    return name, {"role": candidate.role, "note": candidate.note, "payload": payload}


def _score(lane: lanes.Lane, bundle: Path, arms: dict[str, Any]) -> dict[str, Any]:
    scorer = get_scorer(lanes.require_scorer(lane))
    manifest = bundles.verify_bundle(bundle)
    report = scorer.score_bundle(bundle, arms)
    report["bundle"] = {
        "lane": manifest["lane"],
        "version": manifest["version"],
        "bundle_id": manifest["bundle_id"],
    }
    report["table"] = scorer.render_table(report)
    return report


def _emit(report: dict[str, Any], out: str | None) -> None:
    print(report["table"])
    if out:
        Path(out).write_text(json.dumps(report, indent=1), encoding="utf-8")
        print(f"[bench] report -> {out}")


def _verified_bundle(lane: lanes.Lane, args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    """The bundle this command names, verified AND confirmed to be this lane's.

    `verify_bundle` proves a bundle is intact, not that it is the one asked for.
    A key bundle handed to `--lane beatgrid` verified, scored, and could be
    posted under the beatgrid heading carrying the key lane's numbers (Codex P1
    BLOCKING, PR #1582). The version is checked alongside it because the report
    and the round line both quote it.

    This runs BEFORE any candidate is launched and before any arm is loaded, so
    a mismatch costs nothing but the message.
    """
    bundle = _bundle_dir(lane.name, args.version, args.bundle_dir)
    manifest = bundles.verify_bundle(bundle)
    if manifest["lane"] != lane.name or manifest["version"] != args.version:
        raise SystemExit(
            f"[bench] {bundle} is sealed as {manifest['lane']}/{manifest['version']}, but this "
            f"command asked for {lane.name}/{args.version}. Pass a --lane and --version that "
            "match the bundle, or point --bundle-dir at the right one: a round is filed under "
            "the lane it names, so scoring this would misfile the measurement."
        )
    return bundle, manifest


def _cmd_score(args: argparse.Namespace) -> int:
    lane = lanes.get_lane(args.lane)
    # Verify BEFORE reading any arm: a report over a bundle that failed its
    # checksums is worthless, and failing on the arm first would blame the
    # wrong file.
    bundle, manifest = _verified_bundle(lane, args)
    arms = dict(_load_arm(lane, spec, manifest["bundle_id"]) for spec in args.arm)
    _emit(_score(lane, bundle, arms), args.out)
    return 0


def run_arm(candidate: lanes.Candidate, manifest: str, workdir: Path, bundle_id: str) -> dict:
    """Run one arm and return its stamped results, or refuse to invent them.

    THE OUTPUT FILE IS DELETED FIRST and required to reappear. A candidate that
    exits 0 without writing would otherwise leave the PREVIOUS run's JSON at
    this path and the harness would load it as fresh evidence, posting a
    regression as a result (Codex P1 BLOCKING, PR #1582).
    """
    out = workdir / f"{candidate.name}.json"
    out.unlink(missing_ok=True)
    command = candidate.command(fixtures=manifest, out=str(out))
    print(f"[bench] {candidate.name}: {' '.join(command)}", flush=True)
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise SystemExit(
            f"[bench] arm {candidate.name} exited {completed.returncode}. A round is "
            "not posted with a missing arm; fix the candidate or drop it from the round."
        )
    if not out.exists():
        raise SystemExit(
            f"[bench] arm {candidate.name} exited 0 but wrote no {out}. Nothing is "
            "scored from a run that produced no results file."
        )
    return _stamp_arm(out, bundle_id)


def _cmd_run(args: argparse.Namespace) -> int:
    lane = lanes.get_lane(args.lane)
    lanes.require_scorer(lane)
    if args.post and args.no_controls:
        raise rounds.RoundError(
            "--no-controls is for a quick uncontrolled look; a round with no floor "
            "and no ceiling cannot be posted. Drop one flag or the other."
        )
    bundle, sealed = _verified_bundle(lane, args)
    manifest = str(bundle / bundles.MANIFEST_NAME)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    wanted = [lanes.get_candidate(lane, args.candidate)]
    if not args.no_controls:
        wanted.extend(c for c in lanes.controls(lane) if c.name != args.candidate)

    arms: dict[str, Any] = {
        candidate.name: {
            "role": candidate.role,
            "note": candidate.note,
            "payload": run_arm(candidate, manifest, workdir, sealed["bundle_id"]),
        }
        for candidate in wanted
    }

    report = _score(lane, bundle, arms)
    _emit(report, args.out)
    if args.post:
        log_path = Path(args.log_path or lane.log_path)
        number = rounds.append_round(
            log_path, lane.name, report, floor=lane.round_floor, host=_host()
        )
        print(f"[bench] appended {lane.name} bench round {number} to {log_path}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.analysis_bench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("lanes", help="print the lane registry").set_defaults(func=_cmd_lanes)

    fixtures = sub.add_parser("fixtures", help="bundle lifecycle")
    fixtures.add_argument("action", choices=("build", "seal", "verify", "push", "pull"))
    fixtures.add_argument("--lane", required=True)
    fixtures.add_argument("--version", default="v1")
    fixtures.add_argument("--dir", help="staged directory, for `seal`")
    fixtures.add_argument("--bundle-dir", help="default data/bench/<lane>/<version>")
    fixtures.add_argument("--dest", help="where `pull` writes, default the bundle dir")
    fixtures.add_argument("--store", help=f"default ${stores.STORE_ENV}")
    fixtures.add_argument("--expect-bundle-id", help="refuse a pull that is not this bundle")
    fixtures.set_defaults(func=_cmd_fixtures)

    for name, func in (("score", _cmd_score), ("run", _cmd_run)):
        cmd = sub.add_parser(name)
        cmd.add_argument("--lane", required=True)
        cmd.add_argument("--version", default="v1")
        cmd.add_argument("--bundle-dir")
        cmd.add_argument("--out", help="write the report JSON here")
        cmd.set_defaults(func=func)
        if name == "score":
            cmd.add_argument("--arm", action="append", required=True, metavar="NAME=RESULTS.json")
        else:
            cmd.add_argument("--candidate", required=True)
            cmd.add_argument("--workdir", default=".tmp/analysis_bench")
            cmd.add_argument("--no-controls", action="store_true",
                             help="score without a floor or a ceiling; blocks --post")
            cmd.add_argument("--post", action="store_true",
                             help="append the numbered round to the lane's experiment log")
            cmd.add_argument("--log-path",
                             help="post the round to this log instead of the lane's declared "
                                  "one, for a dry run before the spec edit is committed")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (bundles.BundleError, lanes.LaneError, rounds.RoundError) as exc:
        print(f"[bench] {exc}", file=sys.stderr)
        return 2
