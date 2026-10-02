"""The shard durations ledger is built on main and read by CI, never committed by a PR.

pytest-split balances the five fast-lane shards from `.test_durations`, and the
fast-tier plugin refuses a run whose ledger names too few of the collected tests
(`scripts/pytest_fast_tier.py`). Until Fri 2 Oct 2026 the only way to refresh it was a
PR that committed a new file (#4699, #5048). Every such PR conflicted with every other
PR touching the ledger, and when it went stale the whole merge queue bounced (#5063).

Now one scheduled workflow (`.github/workflows/durations-ledger.yml`) builds the ledger
from main and publishes it as an artifact, and `ci.yml` installs that artifact over the
committed file before the shards split. The committed file stays as the seed that a
fresh clone, a local run, or a CI run with no published ledger falls back to.

Subcommands (standard library only, so a bare runner `python3` can run them):

- `build`: union the five `test-durations-shard-N` artifacts of the newest successful
  main-push `ci.yml` run that still has all five, and write the merged ledger.
- `resolve`: print the run id of the newest successful ledger run whose artifact has
  not expired, or nothing. The scope job resolves it ONCE so every shard of a run
  splits on the same ledger: two shards on different ledgers can drop or double-run a
  test without anything going red.
- `install`: validate a downloaded ledger and copy it over the committed one.

A ledger is refused (non-zero exit, nothing written) when its shards overlap, a value is
not a non-negative number, a test is recorded above `MAX_TEST_SECONDS`, or it names
fewer than `MIN_ROW_RATIO` of the committed seed's rows (a partial run).
"""

from __future__ import annotations

import argparse
import io
import json
import math
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

SHARDS = 5
SHARD_ARTIFACT = "test-durations-shard-{n}"
LEDGER_ARTIFACT = "test-durations-ledger"
LEDGER_FILE = ".test_durations"
SOURCE_WORKFLOW = "ci.yml"
LEDGER_WORKFLOW = "durations-ledger.yml"
# Twice the fast lane's per-test pytest-timeout (900 s): a recorded duration above it
# was measured on a broken run, and would starve the shard it lands in.
MAX_TEST_SECONDS = 1800.0
MIN_ROW_RATIO = 0.9
RUNS_TO_SCAN = 20


class LedgerError(Exception):
    """A ledger that must not be published or installed."""


def _validate(ledger: object, label: str) -> dict[str, float]:
    if not isinstance(ledger, dict) or not ledger:
        raise LedgerError(f"{label}: not a non-empty JSON object")
    for node, seconds in ledger.items():
        if not isinstance(node, str) or "::" not in node:
            raise LedgerError(f"{label}: {node!r} is not a pytest node id")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise LedgerError(f"{label}: {node} has a non-numeric duration {seconds!r}")
        if not math.isfinite(seconds) or seconds < 0:
            raise LedgerError(f"{label}: {node} has an invalid duration {seconds!r}")
        if seconds > MAX_TEST_SECONDS:
            raise LedgerError(
                f"{label}: {node} recorded {seconds:.1f} s, above the {MAX_TEST_SECONDS:.0f} s ceiling"
            )
    return ledger


def merge(shards: list[dict[str, float]]) -> dict[str, float]:
    """Union the per-shard ledgers; each test must come from exactly one shard."""
    if len(shards) != SHARDS:
        raise LedgerError(f"expected {SHARDS} shard ledgers, got {len(shards)}")
    merged: dict[str, float] = {}
    for index, shard in enumerate(shards, start=1):
        _validate(shard, f"shard {index}")
        overlap = merged.keys() & shard.keys()
        if overlap:
            raise LedgerError(f"shard {index} repeats {len(overlap)} test(s), e.g. {sorted(overlap)[0]}")
        merged.update(shard)
    return merged


def check_rows(ledger: dict[str, float], seed: dict[str, float]) -> None:
    """Refuse a ledger that names far fewer tests than the committed seed."""
    if len(ledger) < MIN_ROW_RATIO * len(seed):
        raise LedgerError(
            f"ledger names {len(ledger)} tests, under {MIN_ROW_RATIO:.0%} of the committed "
            f"seed's {len(seed)}; refusing a partial run"
        )


def render(ledger: dict[str, float]) -> str:
    """The committed file's format: sorted keys, 4-space indent, trailing newline."""
    return json.dumps(dict(sorted(ledger.items())), indent=4) + "\n"


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LedgerError(f"{path}: unreadable ({exc})") from exc


def _gh(path: str) -> bytes:
    gh = shutil.which("gh")
    if gh is None:
        raise LedgerError("gh is not on PATH")
    done = subprocess.run([gh, "api", path], capture_output=True, timeout=120, check=False)
    if done.returncode != 0:
        raise LedgerError(f"gh api {path} exited {done.returncode}: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def _gh_json(path: str) -> dict:
    return json.loads(_gh(path))


def _live_artifact(repo: str, run_id: int, name: str) -> int | None:
    """The id of a run's unexpired artifact called `name`, or None. Filtered by name, so
    a run with more than one page of artifacts cannot hide it."""
    listing = _gh_json(f"repos/{repo}/actions/runs/{run_id}/artifacts?name={name}&per_page=100")
    live = [a["id"] for a in listing.get("artifacts", []) if a.get("name") == name and not a.get("expired")]
    return live[0] if live else None


def _artifact_file(repo: str, artifact_id: int) -> object:
    blob = _gh(f"repos/{repo}/actions/artifacts/{artifact_id}/zip")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        try:
            return json.loads(archive.read(LEDGER_FILE))
        except KeyError as exc:
            raise LedgerError(f"artifact {artifact_id} has no {LEDGER_FILE}") from exc


def build(repo: str, out: Path, seed_path: Path) -> str:
    seed = _validate(_read_json(seed_path), f"seed {seed_path}")
    runs = _gh_json(
        f"repos/{repo}/actions/workflows/{SOURCE_WORKFLOW}/runs"
        f"?branch=main&event=push&status=success&per_page={RUNS_TO_SCAN}"
    ).get("workflow_runs", [])
    names = [SHARD_ARTIFACT.format(n=n) for n in range(1, SHARDS + 1)]
    skipped: list[str] = []
    for run in runs:
        ids = [_live_artifact(repo, run["id"], name) for name in names]
        if None in ids:
            skipped.append(f"run {run['id']}: shard artifact(s) missing or expired")
            continue
        # One bad run (a short run, a repeated test) must not hide an older good one.
        try:
            ledger = merge([_artifact_file(repo, artifact_id) for artifact_id in ids])
            check_rows(ledger, seed)
        except LedgerError as exc:
            skipped.append(f"run {run['id']}: {exc}")
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(ledger), encoding="utf-8")
        added, dropped = len(ledger.keys() - seed.keys()), len(seed.keys() - ledger.keys())
        return (
            f"[durations-ledger] built {len(ledger)} rows from {SOURCE_WORKFLOW} run {run['id']} "
            f"(main {run['head_sha'][:9]}); vs the committed seed: +{added} / -{dropped}, "
            f"slowest {max(ledger.values()):.1f} s"
        )
    raise LedgerError(
        f"none of the newest {len(runs)} successful main-push {SOURCE_WORKFLOW} runs gave a valid "
        f"ledger from all {SHARDS} shard durations artifacts: " + "; ".join(skipped)
    )


def resolve(repo: str) -> str:
    runs = _gh_json(
        f"repos/{repo}/actions/workflows/{LEDGER_WORKFLOW}/runs?branch=main&status=success&per_page=10"
    ).get("workflow_runs", [])
    for run in runs:
        if _live_artifact(repo, run["id"], LEDGER_ARTIFACT) is not None:
            return str(run["id"])
    return ""


def install(src: Path, dest: Path) -> str:
    seed = _validate(_read_json(dest), f"seed {dest}")
    ledger = _validate(_read_json(src), f"downloaded {src}")
    check_rows(ledger, seed)
    shutil.copyfile(src, dest)
    return (
        f"[durations-ledger] installed {len(ledger)} rows over the committed seed's {len(seed)} "
        f"(+{len(ledger.keys() - seed.keys())} / -{len(seed.keys() - ledger.keys())})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_build = sub.add_parser("build")
    p_build.add_argument("--repo", required=True)
    p_build.add_argument("--out", type=Path, required=True)
    p_build.add_argument("--seed", type=Path, default=Path(LEDGER_FILE))
    p_resolve = sub.add_parser("resolve")
    p_resolve.add_argument("--repo", required=True)
    p_install = sub.add_parser("install")
    p_install.add_argument("--src", type=Path, required=True)
    p_install.add_argument("--dest", type=Path, default=Path(LEDGER_FILE))
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            print(build(args.repo, args.out, args.seed))
        elif args.command == "resolve":
            print(resolve(args.repo))
        else:
            print(install(args.src, args.dest))
    except (LedgerError, OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        # Every failure, expected or not, reaches the log as an annotation that names it.
        print(f"::error title=durations ledger::{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
