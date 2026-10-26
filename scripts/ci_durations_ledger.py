"""The shard durations ledger is built on main and read by CI, never committed by a PR.

pytest-split balances the five fast-lane shards from `.test_durations`, and the
fast-tier plugin refuses a run whose ledger names too few of the collected tests
(`scripts/pytest_fast_tier.py`). Until Fri 2 Oct 2026 the only way to refresh it was a
PR that committed a new file (#4699, #5048). Every such PR conflicted with every other
PR touching the ledger, and when it went stale the whole merge queue bounced (#5063).

Now one scheduled workflow (`.github/workflows/durations-ledger.yml`) builds the ledger
from main and stores it as the only file of a parentless commit on the `ci-data` branch,
and `ci.yml` installs it over the committed file before the shards split. The committed
file stays as the seed that a fresh clone, a local run, or a CI run with no published
ledger falls back to. Until Mon 26 Oct 2026 the ledger travelled as an artifact: once
Actions storage filled and the artifacts expired, the builder failed 37 runs in a row,
coverage fell under the fast tier's floor, and main locked itself red
((private path)).

Subcommands (standard library only, so a bare runner `python3` can run them):

- `build`: union the five `test-durations-shard-N` artifacts of the newest successful
  main-push `ci.yml` run that still has all five, and write the merged ledger. When no
  run qualifies it writes nothing and keeps the published ledger, green with a warning
  while that ledger is under `MAX_LEDGER_AGE_DAYS` old, red beyond it.
- `publish`: store a built ledger on `ci-data` (force-moved, no history kept).
- `resolve`: print the `ci-data` commit sha whose ledger downloads and holds enough of
  the committed seed's rows, or nothing. The scope job resolves it ONCE so every shard
  of a run splits on the same ledger: two shards on different ledgers can drop or
  double-run a test without anything going red.
- `fetch`: download that commit's ledger with `gh api` and install it (what every CI
  shard runs). Every `gh` call is bounded and retried once; a ledger that still cannot be
  fetched fails the shard by name, never falls back.

A ledger is refused (non-zero exit, nothing written) when its shards overlap, a value is
not a non-negative number, a test is recorded above `MAX_TEST_SECONDS`, or it names
fewer than `MIN_ROW_RATIO` of the committed seed's rows (a partial run).

Supersedes: the hand-made refresh PR that committed a new `.test_durations` (the
"Refresh procedure" in (private path), round 4; #4699, #5048). That section
now redirects to this module and its workflow. Decision record:
(private path), superseded in
its storage by ADR-NEW-durations-ledger-on-a-ci-data-branch.md.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

SHARDS = 5
SHARD_ARTIFACT = "test-durations-shard-{n}"
LEDGER_FILE = ".test_durations"
SOURCE_WORKFLOW = "ci.yml"
LEDGER_WORKFLOW = "durations-ledger.yml"
# The ledger lives on this branch, outside the artifact system ((private path)).
DATA_BRANCH = "ci-data"
# The builder keeps a published ledger this old when it cannot build a fresh one.
MAX_LEDGER_AGE_DAYS = 14
# Twice the fast lane's per-test pytest-timeout (900 s): a recorded duration above it
# was measured on a broken run, and would starve the shard it lands in.
MAX_TEST_SECONDS = 1800.0
MIN_ROW_RATIO = 0.9
RUNS_TO_SCAN = 20
# fetch: two attempts of three calls each (commit, tree, blob) must fit its 2-minute
# CI step with 15 s to spare.
FETCH_GH_TIMEOUT_S = 15
FETCH_ATTEMPTS = 2

# The one network seam: `gh api <path>` -> response body. Tests replay real captured
# exchanges through it ((private path)).
Api = Callable[[str], bytes]
# The one write seam: (method, path, JSON body) -> response body.
Send = Callable[[str, str, dict], bytes]


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
            raise LedgerError(f"{label}: {node} recorded {seconds:.1f} s, above the {MAX_TEST_SECONDS:.0f} s ceiling")
    return ledger


def merge(shards: Sequence[object]) -> dict[str, float]:
    """Union the per-shard ledgers; each test must come from exactly one shard."""
    if len(shards) != SHARDS:
        raise LedgerError(f"expected {SHARDS} shard ledgers, got {len(shards)}")
    merged: dict[str, float] = {}
    for index, raw in enumerate(shards, start=1):
        shard = _validate(raw, f"shard {index}")
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


def _gh(path: str, timeout: float = 120) -> bytes:
    gh = shutil.which("gh")
    if gh is None:
        raise LedgerError("gh is not on PATH")
    done = subprocess.run([gh, "api", path], capture_output=True, timeout=timeout, check=False)
    if done.returncode != 0:
        raise LedgerError(f"gh api {path} exited {done.returncode}: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def _gh_json(api: Api, path: str) -> dict:
    return json.loads(api(path))


def _live_artifact(repo: str, run_id: int, name: str, api: Api = _gh) -> int | None:
    """The id of a run's unexpired artifact called `name`, or None. Filtered by name, so
    a run with more than one page of artifacts cannot hide it."""
    listing = _gh_json(api, f"repos/{repo}/actions/runs/{run_id}/artifacts?name={name}&per_page=100")
    live = [a["id"] for a in listing.get("artifacts", []) if a.get("name") == name and not a.get("expired")]
    return live[0] if live else None


def _artifact_file(repo: str, artifact_id: int, api: Api = _gh) -> object:
    blob = api(f"repos/{repo}/actions/artifacts/{artifact_id}/zip")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        try:
            return json.loads(archive.read(LEDGER_FILE))
        except KeyError as exc:
            raise LedgerError(f"artifact {artifact_id} has no {LEDGER_FILE}") from exc


def build(repo: str, out: Path, seed_path: Path, api: Api = _gh) -> str:
    seed = _validate(_read_json(seed_path), f"seed {seed_path}")
    runs = _gh_json(
        api,
        f"repos/{repo}/actions/workflows/{SOURCE_WORKFLOW}/runs"
        f"?branch=main&event=push&status=success&per_page={RUNS_TO_SCAN}",
    ).get("workflow_runs", [])
    names = [SHARD_ARTIFACT.format(n=n) for n in range(1, SHARDS + 1)]
    skipped: list[str] = []
    for run in runs:
        found = [_live_artifact(repo, run["id"], name, api) for name in names]
        ids = [artifact_id for artifact_id in found if artifact_id is not None]
        if len(ids) != len(names):
            skipped.append(f"run {run['id']}: shard artifact(s) missing or expired")
            continue
        # One bad run (a short run, a repeated test) must not hide an older good one.
        try:
            ledger = merge([_artifact_file(repo, artifact_id, api) for artifact_id in ids])
            check_rows(ledger, seed)
        except LedgerError as exc:
            skipped.append(f"run {run['id']}: {exc}")
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(ledger), encoding="utf-8", newline="\n")
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


def _gh_send(method: str, path: str, body: dict, timeout: float = 120) -> bytes:
    """`gh api -X <method> <path>` with a JSON body on stdin (the ledger is 2.5 MB)."""
    gh = shutil.which("gh")
    if gh is None:
        raise LedgerError("gh is not on PATH")
    done = subprocess.run(
        [gh, "api", "-X", method, path, "--input", "-"],
        input=json.dumps(body).encode(),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if done.returncode != 0:
        raise LedgerError(
            f"gh api -X {method} {path} exited {done.returncode}: {done.stderr.decode(errors='replace').strip()}"
        )
    return done.stdout


def publish(repo: str, ledger_path: Path, send: Send = _gh_send) -> str:
    """Store the built ledger as the only file of a parentless commit on `DATA_BRANCH`.

    Parentless and force-moved, so the branch never accumulates history: one ledger,
    its commit date is when it was built. Outside the artifact system on purpose: an
    artifact expires (1 to 7 days) and cannot be written once Actions storage is full,
    and both together locked main red from Sat 17 Oct 2026 ((private path)).
    """
    ledger = _validate(_read_json(ledger_path), f"built ledger {ledger_path}")
    blob = json.loads(send("POST", f"repos/{repo}/git/blobs", {"content": render(ledger), "encoding": "utf-8"}))
    tree = json.loads(
        send(
            "POST",
            f"repos/{repo}/git/trees",
            {"tree": [{"path": LEDGER_FILE, "mode": "100644", "type": "blob", "sha": blob["sha"]}]},
        )
    )
    commit = json.loads(
        send(
            "POST",
            f"repos/{repo}/git/commits",
            {"message": f"durations ledger: {len(ledger)} rows", "tree": tree["sha"], "parents": []},
        )
    )
    try:
        send("PATCH", f"repos/{repo}/git/refs/heads/{DATA_BRANCH}", {"sha": commit["sha"], "force": True})
    except LedgerError as moved:
        # First publish: the branch does not exist yet, so create it.
        try:
            send("POST", f"repos/{repo}/git/refs", {"ref": f"refs/heads/{DATA_BRANCH}", "sha": commit["sha"]})
        except LedgerError as created:
            raise LedgerError(f"could not move or create {DATA_BRANCH}: {moved}; {created}") from created
    return f"[durations-ledger] published {len(ledger)} rows to {DATA_BRANCH} at {commit['sha']}"


def _published_ref(repo: str, api: Api) -> str:
    ref = _gh_json(api, f"repos/{repo}/git/ref/heads/{DATA_BRANCH}")
    sha = ref["object"]["sha"]
    if not isinstance(sha, str) or len(sha) != 40:
        raise LedgerError(f"{DATA_BRANCH} points at {sha!r}, not a commit sha")
    return sha


def _ledger_at(repo: str, sha: str, api: Api) -> tuple[object, datetime]:
    """The ledger stored in commit `sha` of `DATA_BRANCH`, and when it was built."""
    commit = _gh_json(api, f"repos/{repo}/git/commits/{sha}")
    built = datetime.fromisoformat(commit["committer"]["date"])
    tree = _gh_json(api, f"repos/{repo}/git/trees/{commit['tree']['sha']}")
    blobs = [entry["sha"] for entry in tree.get("tree", []) if entry.get("path") == LEDGER_FILE]
    if not blobs:
        raise LedgerError(f"{DATA_BRANCH} commit {sha} has no {LEDGER_FILE}")
    blob = _gh_json(api, f"repos/{repo}/git/blobs/{blobs[0]}")
    return json.loads(base64.b64decode(blob["content"])), built


def keep_published(repo: str, now: datetime, reason: str, api: Api = _gh) -> str:
    """No fresh ledger could be built: keep the published one while it is young enough.

    A stale ledger only balances the shards less evenly; it never decides a verdict. So
    the builder stays green on a ledger up to `MAX_LEDGER_AGE_DAYS` old and says how old,
    and only goes red beyond that, naming why nothing fresh was built.
    """
    try:
        sha = _published_ref(repo, api)
        _ledger, built = _ledger_at(repo, sha, api)
    except (LedgerError, KeyError, ValueError) as exc:
        raise LedgerError(f"{reason}; and no published ledger to keep ({type(exc).__name__}: {exc})") from exc
    age = now - built
    days = age.total_seconds() / 86400
    if age > timedelta(days=MAX_LEDGER_AGE_DAYS):
        raise LedgerError(
            f"{reason}; the published ledger ({sha[:9]}) is {days:.1f} days old, over the "
            f"{MAX_LEDGER_AGE_DAYS}-day limit"
        )
    print(
        f"::warning title=durations ledger is not refreshing::{reason}; keeping the published "
        f"ledger {sha[:9]}, {days:.1f} days old ({MAX_LEDGER_AGE_DAYS}-day limit)",
        file=sys.stderr,
    )
    return f"[durations-ledger] kept {DATA_BRANCH} {sha[:9]}, built {built.isoformat()}"


def resolve(repo: str, seed_path: Path, api: Api = _gh) -> str:
    """The `DATA_BRANCH` commit sha every shard of this run installs, or "" for the seed."""
    # The seed is required: without it resolve could name a ledger `fetch` then refuses.
    seed = _validate(_read_json(seed_path), f"seed {seed_path}")
    try:
        sha = _published_ref(repo, api)
    except LedgerError as exc:
        print(f"::warning title=durations ledger::no published ledger ({exc})", file=sys.stderr)
        return ""
    # Prove the ledger downloads with this run's token, and that `fetch` would accept it
    # against this checkout's seed, before every shard depends on it: a probe that still
    # fails here sends ALL shards to the seed together, which is safe; shards failing one
    # by one is not (Mon 19 Oct 2026, an artifact 503 to every shard).
    errors: list[str] = []
    for _attempt in range(FETCH_ATTEMPTS):
        try:
            raw, _built = _ledger_at(repo, sha, api)
            ledger = _validate(raw, f"{DATA_BRANCH} {sha[:9]}")
        except (LedgerError, subprocess.TimeoutExpired, KeyError, ValueError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            continue
        try:
            check_rows(ledger, seed)
        except LedgerError as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            break
        return sha
    print(
        f"::warning title=durations ledger::{DATA_BRANCH} {sha[:9]}'s ledger is not usable "
        f"({'; '.join(errors)}); every shard splits on the committed seed",
        file=sys.stderr,
    )
    return ""


def fetch(repo: str, sha: str, dest: Path, api: Api = _gh) -> str:
    """Install the ledger in `DATA_BRANCH` commit `sha` over `dest`. Network errors are
    retried; a ledger that arrives but fails validation is not, it is refused."""
    seed = _validate(_read_json(dest), f"seed {dest}")
    errors: list[str] = []
    for _attempt in range(FETCH_ATTEMPTS):
        try:
            raw, _built = _ledger_at(repo, sha, api)
        except (LedgerError, subprocess.TimeoutExpired, KeyError, ValueError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            continue
        ledger = _validate(raw, f"{DATA_BRANCH} {sha[:9]}")
        check_rows(ledger, seed)
        dest.write_text(render(ledger), encoding="utf-8", newline="\n")
        return (
            f"[durations-ledger] installed {len(ledger)} rows from {DATA_BRANCH} {sha[:9]} over the "
            f"committed seed's {len(seed)} (+{len(ledger.keys() - seed.keys())} / -{len(seed.keys() - ledger.keys())})"
            + (f" after {len(errors)} failed attempt(s)" if errors else "")
        )
    raise LedgerError(
        f"could not fetch {DATA_BRANCH} {sha[:9]}'s ledger in {FETCH_ATTEMPTS} attempts: " + "; ".join(errors)
    )


def _bounded_gh(path: str) -> bytes:
    return _gh(path, timeout=FETCH_GH_TIMEOUT_S)


def main(argv: list[str] | None = None, api: Api = _gh, send: Send = _gh_send, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_build = sub.add_parser("build")
    p_build.add_argument("--repo", required=True)
    p_build.add_argument("--out", type=Path, required=True)
    p_build.add_argument("--seed", type=Path, default=Path(LEDGER_FILE))
    p_publish = sub.add_parser("publish")
    p_publish.add_argument("--repo", required=True)
    p_publish.add_argument("--ledger", type=Path, required=True)
    p_resolve = sub.add_parser("resolve")
    p_resolve.add_argument("--repo", required=True)
    p_resolve.add_argument("--seed", type=Path, default=Path(LEDGER_FILE))
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--repo", required=True)
    p_fetch.add_argument("--ref", required=True)
    p_fetch.add_argument("--dest", type=Path, default=Path(LEDGER_FILE))
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            try:
                print(build(args.repo, args.out, args.seed, api))
            except LedgerError as exc:
                # Nothing is written to --out, so the workflow's publish step skips.
                print(keep_published(args.repo, now or datetime.now(UTC), str(exc), api))
        elif args.command == "publish":
            print(publish(args.repo, args.ledger, send))
        elif args.command == "resolve":
            print(resolve(args.repo, args.seed, api))
        else:
            print(fetch(args.repo, args.ref, args.dest, _bounded_gh if api is _gh else api))
    except (LedgerError, OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        # Every failure, expected or not, reaches the log as an annotation that names it.
        print(f"::error title=durations ledger::{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
