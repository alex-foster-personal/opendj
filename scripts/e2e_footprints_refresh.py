"""The root Playwright footprint map: built from a full run, published on `ci-data-e2e`.

`scripts/ci_e2e_selection.py` narrows the e2e gate's root suite to the specs whose
measured footprint overlaps a PR's changed files (TEST-BUDGET round 3). The footprint
is measured, never derived: a full root run with `E2E_FOOTPRINTS_DIR` set makes the
dev server log every `src/` module the browser fetches
(`apps/webui/frontend/vite-e2e-footprints.ts`) and the footprint reporter log when each
spec's tests ran (`tests/e2e/support/footprint-reporter.ts`). `build` joins the two.

Maintained the way `.test_durations` is (scripts/ci_durations_ledger.py): the e2e gate
uploads a map from every full root run on a push to main as the R2 artifact
`e2e-spec-footprints`; `durations-ledger.yml` publishes the newest one as the only
file of a parentless commit on the `ci-data-e2e` branch every six hours; the scope job
fetches that commit's map, and falls back to the committed seed
`apps/webui/frontend/tests/e2e/.spec_footprints.json` when nothing is published. A PR
never commits a refreshed map to refresh it: the seed is replaced by hand only when
the format changes or the published map is lost (`run` below builds one).

Subcommands (standard library only, so a bare runner `python3` can run them):

- `build`: join `$E2E_FOOTPRINTS_DIR` (requests.jsonl, tests.jsonl, collected.json) into
  a map for the given SHA. Refuses an empty log on either side: a run that collected
  nothing is not a map.
- `publish`: store a built map on `ci-data-e2e` (force-moved, no history kept).
- `fetch`: download the published map with `gh api` and write it to `--dest`; prints
  its SHA and build time. Nothing written on any failure, so the scope job's fallback
  to the seed is the absence of the file.
- `ledger`: the scheduled half: find the newest successful main-push `e2e.yml` run whose
  `e2e-spec-footprints` artifact is on R2 and write its map to `--out`; nothing written
  when none qualifies (the published map is kept, red only past `MAX_MAP_AGE_DAYS`).
- `run`: run the root suite locally with footprints on and build the seed. The run is
  a full `pnpm test:e2e` (about 10 minutes on a Linux runner) and needs the engine's
  Python environment; run it on a host that allows test browsers.

Map format (schema 1, `(private path)` pins it):
    {"schema": 1, "sha": <40 hex>, "built": <ISO-8601 UTC>, "paths": [<src/...>],
     "ignored": [<tests/e2e/*.spec.ts on disk the root config did not collect>],
     "specs": {<tests/e2e/x.spec.ts>: {"status": passed|skipped|unmeasured,
                                        "seconds": <float>, "tests": <int>,
                                        "files": [<index into paths>]}},
     "gates": {<name in GATES>: {"status": ..., "seconds": <float>, "specs": [...],
                                 "files": [<index into paths>]}}}
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from scripts import r2_artifact

SCHEMA = 1
MAP_FILE = ".spec_footprints.json"
DATA_BRANCH = "ci-data-e2e"
ARTIFACT_NAME = "e2e-spec-footprints"
SOURCE_WORKFLOW = "e2e.yml"
FRONTEND = "apps/webui/frontend"
SEED = f"{FRONTEND}/tests/e2e/{MAP_FILE}"
REQUESTS_LOG = "requests.jsonl"
TESTS_LOG = "tests.jsonl"
COLLECTED = "collected.json"
REPORTER = "./tests/e2e/support/footprint-reporter.ts"
GATES_DIR = "gates"
#: The e2e gate's named Playwright steps that boot a Vite dev server, so the same plugin and
#: reporter footprint them: name -> (config under tests/e2e, extra CLI arguments). The names
#: are what the selector prints in `e2e_gates` and what e2e.yml's step conditions test. The
#: production-build and no-server gates (webkit, deckload-smoke, stretch, master12,
#: onboarding) cannot be footprinted this way; `scripts.ci_e2e_selection` lists them as
#: static gates.
GATES: dict[str, tuple[str, tuple[str, ...]]] = {
    "rekordbox": ("playwright.rekordbox-gate.config.ts", ()),
    "comment-hotkey": ("playwright.comment-hotkey-gate.config.ts", ()),
    "autoplay": ("playwright.autoplay-stall-gate.config.ts", ()),
    "headphone": (
        "playwright.performance.config.ts",
        (
            "tests/e2e/performance-headphone-device-probe.spec.ts",
            "tests/e2e/performance-library-panels.spec.ts",
        ),
    ),
    "library-jobs": ("playwright.library-jobs.config.ts", ()),
    "preflight": ("playwright.preflight-gate.config.ts", ()),
}
MAX_MAP_AGE_DAYS = 7
RUNS_TO_SCAN = 20
FETCH_GH_TIMEOUT_S = 15
HEX40 = re.compile(r"^[0-9a-f]{40}$")

Api = Callable[[str], bytes]
Send = Callable[[str, str, dict], bytes]


class ArtifactStore(Protocol):
    """The artifact read seam, shaped like `scripts.r2_artifact` with credentials bound."""

    def exists(self, key: str) -> bool: ...
    def read_member(self, key: str, member: str) -> bytes: ...


class R2Store:
    """`scripts.r2_artifact` on the env credentials (R2_*), read per call so a subcommand
    that never touches an artifact needs none."""

    def exists(self, key: str) -> bool:
        return r2_artifact.exists(r2_artifact._creds_from_env(), key)

    def read_member(self, key: str, member: str) -> bytes:
        return r2_artifact.read_member(r2_artifact._creds_from_env(), key, member)


class MapError(Exception):
    """A map that must not be published, installed or selected over."""


# ---------------------------------------------------------------------------
# build: join the two logs
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MapError(f"{path}: unreadable ({exc})") from exc
    rows = [json.loads(line) for line in lines if line.strip()]
    if not rows:
        raise MapError(f"{path}: empty; the run recorded nothing (is the other half switched on?)")
    return rows


def _spec_windows(tests: list[dict]) -> list[tuple[str, int, int, str, float, int]]:
    """Per spec, in run order: (spec, start, end, status, seconds, tests).

    Every gap between two specs is handed to BOTH neighbors, and the run's two ends to
    the first and last spec: a `beforeAll` page that loads before the first test's clock
    starts, or an `afterAll` after the last one, lands in a gap, and over-attributing it
    is safe where dropping it is not.
    """
    by_spec: dict[str, list[dict]] = {}
    for row in tests:
        by_spec.setdefault(row["spec"], []).append(row)
    windows = []
    for spec, rows in by_spec.items():
        statuses = {row["status"] for row in rows}
        if statuses <= {"skipped"}:
            status = "skipped"
        elif statuses <= {"passed", "skipped"}:
            status = "passed"
        else:
            status = "unmeasured"
        seconds = sum(row["end"] - row["start"] for row in rows) / 1000
        windows.append((spec, min(r["start"] for r in rows), max(r["end"] for r in rows), status, seconds, len(rows)))
    windows.sort(key=lambda w: w[1])
    widened = []
    for index, (spec, start, end, status, seconds, count) in enumerate(windows):
        previous_end = windows[index - 1][2] if index > 0 else 0
        next_start = windows[index + 1][1] if index + 1 < len(windows) else sys.maxsize
        widened.append((spec, min(previous_end, start), max(next_start, end), status, seconds, count))
    return widened


def _run_logs(run_dir: Path) -> tuple[list[dict], list[dict], list[str]]:
    requests = _read_jsonl(run_dir / REQUESTS_LOG)
    tests = _read_jsonl(run_dir / TESTS_LOG)
    collected = json.loads((run_dir / COLLECTED).read_text(encoding="utf-8"))
    return requests, tests, collected


def _footprints_of(requests: list[dict], tests: list[dict], index_of: dict[str, int]) -> dict[str, dict]:
    stamps = sorted((row["t"], index_of[row["path"]]) for row in requests)
    specs: dict[str, dict] = {}
    for spec, start, end, ran, seconds, count in _spec_windows(tests):
        files = sorted({i for t, i in stamps if start <= t <= end})
        # Ran and fetched no source: a harness bundle the request log cannot see, or a spec
        # that only talks to the engine. Unmeasured, so the selector always runs it.
        status = "unmeasured" if ran == "passed" and not files else ran
        specs[spec] = {"status": status, "seconds": round(seconds, 1), "tests": count, "files": files}
    return specs


def _gate_entry(specs: dict[str, dict], collected: list[str]) -> dict:
    """One named gate as a whole: its status, seconds and the union of its specs' files."""
    statuses = {entry["status"] for entry in specs.values()}
    if set(collected) - set(specs) or "unmeasured" in statuses:
        status = "unmeasured"
    elif statuses <= {"skipped"}:
        status = "skipped"
    else:
        status = "passed"
    return {
        "status": status,
        "seconds": round(sum(entry["seconds"] for entry in specs.values()), 1),
        "specs": sorted(collected),
        "files": sorted({index for entry in specs.values() for index in entry["files"]}),
    }


def build(footprints_dir: Path, sha: str, repo_root: Path, now: datetime) -> dict:
    """The map from a run's logs: the root suite at the top of `footprints_dir`, and one
    named gate per `gates/<name>/` directory beneath it (absent gates stay unmeasured)."""
    if not HEX40.match(sha):
        raise MapError(f"sha {sha!r} is not a 40-hex commit")
    requests, tests, collected = _run_logs(footprints_dir)
    gate_logs = {
        name: _run_logs(footprints_dir / GATES_DIR / name)
        for name in sorted(GATES)
        if (footprints_dir / GATES_DIR / name / TESTS_LOG).exists()
    }
    gate_requests = [row for logs in gate_logs.values() for row in logs[0]]
    paths = sorted({row["path"] for row in requests} | {row["path"] for row in gate_requests})
    index_of = {path: i for i, path in enumerate(paths)}
    specs = _footprints_of(requests, tests, index_of)
    for spec in collected:
        specs.setdefault(spec, {"status": "unmeasured", "seconds": 0.0, "tests": 0, "files": []})
    gates = {name: _gate_entry(_footprints_of(logs[0], logs[1], index_of), logs[2]) for name, logs in gate_logs.items()}
    on_disk = sorted(f"tests/e2e/{p.name}" for p in (repo_root / FRONTEND / "tests" / "e2e").glob("*.spec.ts"))
    ignored = [spec for spec in on_disk if spec not in specs]
    return {
        "schema": SCHEMA,
        "sha": sha,
        "built": now.isoformat(timespec="seconds"),
        "paths": paths,
        "ignored": ignored,
        "specs": dict(sorted(specs.items())),
        "gates": gates,
    }


def render(footprints: dict) -> str:
    return json.dumps(footprints, indent=1, separators=(",", ": ")) + "\n"


def validate(raw: object, label: str) -> dict:
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise MapError(f"{label}: not a schema {SCHEMA} footprint map")
    if not isinstance(raw.get("sha"), str) or not HEX40.match(raw["sha"]):
        raise MapError(f"{label}: sha is not a 40-hex commit")
    datetime.fromisoformat(str(raw.get("built")))
    if not isinstance(raw.get("specs"), dict) or not raw["specs"]:
        raise MapError(f"{label}: no specs")
    if not isinstance(raw.get("paths"), list) or not raw["paths"]:
        raise MapError(f"{label}: no paths")
    if not isinstance(raw.get("gates", {}), dict):
        raise MapError(f"{label}: gates is not an object")
    return raw


# ---------------------------------------------------------------------------
# the GitHub seams (ci_durations_ledger's shape, so the tests replay exchanges)
# ---------------------------------------------------------------------------


def _gh(path: str, timeout: float = 120) -> bytes:
    gh = shutil.which("gh")
    if gh is None:
        raise MapError("gh is not on PATH")
    done = subprocess.run([gh, "api", path], capture_output=True, timeout=timeout, check=False)
    if done.returncode != 0:
        raise MapError(f"gh api {path} exited {done.returncode}: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def _gh_send(method: str, path: str, body: dict, timeout: float = 120) -> bytes:
    gh = shutil.which("gh")
    if gh is None:
        raise MapError("gh is not on PATH")
    done = subprocess.run(
        [gh, "api", "-X", method, path, "--input", "-"],
        input=json.dumps(body).encode(),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if done.returncode != 0:
        raise MapError(
            f"gh api -X {method} {path} exited {done.returncode}: {done.stderr.decode(errors='replace').strip()}"
        )
    return done.stdout


def _bounded_gh(path: str) -> bytes:
    return _gh(path, timeout=FETCH_GH_TIMEOUT_S)


def publish(repo: str, map_path: Path, send: Send = _gh_send) -> str:
    footprints = validate(json.loads(map_path.read_text(encoding="utf-8")), f"built map {map_path}")
    blob = json.loads(send("POST", f"repos/{repo}/git/blobs", {"content": render(footprints), "encoding": "utf-8"}))
    tree = json.loads(
        send(
            "POST",
            f"repos/{repo}/git/trees",
            {"tree": [{"path": MAP_FILE, "mode": "100644", "type": "blob", "sha": blob["sha"]}]},
        )
    )
    message = f"e2e footprint map: {len(footprints['specs'])} specs at {footprints['sha'][:9]}"
    commit = json.loads(
        send("POST", f"repos/{repo}/git/commits", {"message": message, "tree": tree["sha"], "parents": []})
    )
    try:
        send("PATCH", f"repos/{repo}/git/refs/heads/{DATA_BRANCH}", {"sha": commit["sha"], "force": True})
    except MapError as moved:
        try:
            send("POST", f"repos/{repo}/git/refs", {"ref": f"refs/heads/{DATA_BRANCH}", "sha": commit["sha"]})
        except MapError as created:
            raise MapError(f"could not move or create {DATA_BRANCH}: {moved}; {created}") from created
    return f"[e2e-footprints] published {len(footprints['specs'])} specs to {DATA_BRANCH} at {commit['sha']}"


def _published(repo: str, api: Api) -> tuple[dict, datetime, str]:
    ref = json.loads(api(f"repos/{repo}/git/ref/heads/{DATA_BRANCH}"))
    sha = ref["object"]["sha"]
    commit = json.loads(api(f"repos/{repo}/git/commits/{sha}"))
    tree = json.loads(api(f"repos/{repo}/git/trees/{commit['tree']['sha']}"))
    blobs = [entry["sha"] for entry in tree.get("tree", []) if entry.get("path") == MAP_FILE]
    if not blobs:
        raise MapError(f"{DATA_BRANCH} commit {sha} has no {MAP_FILE}")
    blob = json.loads(api(f"repos/{repo}/git/blobs/{blobs[0]}"))
    raw = json.loads(base64.b64decode(blob["content"]))
    return validate(raw, f"{DATA_BRANCH} {sha[:9]}"), datetime.fromisoformat(commit["committer"]["date"]), sha


def fetch(repo: str, dest: Path, api: Api = _bounded_gh) -> str:
    footprints, committed, sha = _published(repo, api)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render(footprints), encoding="utf-8", newline="\n")
    return (
        f"[e2e-footprints] fetched {len(footprints['specs'])} specs built at {footprints['built']} "
        f"on {footprints['sha'][:9]} from {DATA_BRANCH} {sha[:9]} (committed {committed.isoformat()})"
    )


# ---------------------------------------------------------------------------
# ledger: the newest main-push run's artifact
# ---------------------------------------------------------------------------


def ledger(repo: str, out: Path, now: datetime, api: Api = _gh, store: ArtifactStore | None = None) -> str:
    """Write the newest qualifying main-push run's map to `out`, or keep the published one."""
    store = R2Store() if store is None else store
    runs = json.loads(
        api(
            f"repos/{repo}/actions/workflows/{SOURCE_WORKFLOW}/runs"
            f"?branch=main&event=push&status=success&per_page={RUNS_TO_SCAN}"
        )
    ).get("workflow_runs", [])
    skipped: list[str] = []
    for run in runs:
        key = r2_artifact.object_key(repo, ARTIFACT_NAME, str(run["id"]))
        try:
            present = store.exists(key)
        except r2_artifact.R2Error as exc:
            raise MapError(f"R2 lookup of {key} failed: {exc}") from exc
        if not present:
            skipped.append(f"run {run['id']}: no {ARTIFACT_NAME} artifact")
            continue
        try:
            raw = store.read_member(key, MAP_FILE)
        except r2_artifact.R2Error as exc:
            raise MapError(f"R2 read of {key}:{MAP_FILE} failed: {exc}") from exc
        try:
            footprints = validate(json.loads(raw), f"run {run['id']}")
        except (MapError, ValueError) as exc:
            skipped.append(f"run {run['id']}: {exc}")
            continue
        days = _age_days(footprints, now)
        if days > MAX_MAP_AGE_DAYS:
            # Republishing it would renew the publication date of a map the selector
            # already rejects by `built`, and keep this workflow green while every
            # in-scope PR runs FULL (Codex, PR #7948).
            skipped.append(f"run {run['id']}: map built {days:.1f} days ago, over the {MAX_MAP_AGE_DAYS}-day limit")
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(footprints), encoding="utf-8", newline="\n")
        return f"[e2e-footprints] built from {SOURCE_WORKFLOW} run {run['id']} (main {run['head_sha'][:9]}): {len(footprints['specs'])} specs"
    reason = (
        f"none of the newest {len(runs)} successful main-push {SOURCE_WORKFLOW} runs has a valid map: "
        + "; ".join(skipped)
    )
    try:
        footprints, committed, sha = _published(repo, api)
    except (MapError, KeyError, ValueError) as exc:
        raise MapError(f"{reason}; and no published map to keep ({type(exc).__name__}: {exc})") from exc
    # Age by `built`, the stamp the selector checks: the publication commit's date only
    # says when this workflow last ran.
    days = _age_days(footprints, now)
    if days > MAX_MAP_AGE_DAYS:
        raise MapError(
            f"{reason}; the published map ({sha[:9]}, committed {committed.isoformat()}) was built "
            f"{days:.1f} days ago, over the {MAX_MAP_AGE_DAYS}-day limit"
        )
    print(
        f"::warning title=e2e footprint map is not refreshing::{reason}; keeping {DATA_BRANCH} {sha[:9]}, "
        f"built {days:.1f} days ago ({MAX_MAP_AGE_DAYS}-day limit)",
        file=sys.stderr,
    )
    return f"[e2e-footprints] kept {DATA_BRANCH} {sha[:9]}, built {footprints['built']}"


def _age_days(footprints: dict, now: datetime) -> float:
    return (now - datetime.fromisoformat(str(footprints["built"]))).total_seconds() / 86400


# ---------------------------------------------------------------------------
# run: a local full run with footprints on, then build the seed
# ---------------------------------------------------------------------------


def run(repo_root: Path, out: Path, now: datetime, gates: Sequence[str] = ()) -> str:
    """`pnpm test:e2e` with both halves on, then `build`.

    Exit 0 (all green) and exit 1 (some tests failed) both build: a failed spec is recorded
    `unmeasured` and the selector runs it on every head, which is the map format's answer to
    a flake on the building host. Any other exit (a crash, no tests found) is not a map.
    """
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise MapError("pnpm is not on PATH")
    git = shutil.which("git")
    if git is None:
        raise MapError("git is not on PATH")
    sha = subprocess.run(
        [git, "-C", str(repo_root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    # A fresh worktree has no .svelte-kit/tsconfig.json until this runs, and Vite then
    # cannot resolve `$lib` (the frontend's first gate step, apps/webui/frontend/CLAUDE.md).
    subprocess.run([pnpm, "exec", "svelte-kit", "sync"], cwd=repo_root / FRONTEND, check=True)
    with tempfile.TemporaryDirectory(prefix="e2e-footprints-") as tmp:
        env = {**os.environ, "E2E_FOOTPRINTS_DIR": tmp}
        done = subprocess.run(
            [pnpm, "test:e2e", f"--reporter=list,junit,{REPORTER}"],
            cwd=repo_root / FRONTEND,
            env=env,
            check=False,
        )
        if done.returncode not in (0, 1):
            raise MapError(f"pnpm test:e2e exited {done.returncode}; a run that did not complete is not a map")
        for name in gates:
            config, extra = GATES[name]
            gate_done = subprocess.run(
                [
                    pnpm,
                    "exec",
                    "playwright",
                    "test",
                    "--config",
                    f"tests/e2e/{config}",
                    *extra,
                    f"--reporter=list,{REPORTER}",
                ],
                cwd=repo_root / FRONTEND,
                env={**os.environ, "E2E_FOOTPRINTS_DIR": str(Path(tmp) / GATES_DIR / name)},
                check=False,
            )
            if gate_done.returncode not in (0, 1):
                raise MapError(f"gate {name} exited {gate_done.returncode}; a run that did not complete is not a map")
        footprints = build(Path(tmp), sha, repo_root, now)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(footprints), encoding="utf-8", newline="\n")
    unmeasured = sorted(spec for spec, entry in footprints["specs"].items() if entry["status"] == "unmeasured")
    unmeasured += sorted(
        f"gate {name}" for name, entry in footprints["gates"].items() if entry["status"] == "unmeasured"
    )
    if unmeasured:
        print(
            f"::warning title=e2e footprint map::{len(unmeasured)} spec(s) failed or never ran and are recorded "
            "unmeasured (they run on every narrowed head): " + ", ".join(unmeasured),
            file=sys.stderr,
        )
    return (
        f"[e2e-footprints] wrote {out}: {len(footprints['specs'])} specs, {len(footprints['paths'])} paths at {sha[:9]}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None, api: Api = _gh, send: Send = _gh_send, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_build = sub.add_parser("build")
    p_build.add_argument("--dir", type=Path, required=True, help="the E2E_FOOTPRINTS_DIR of a full run")
    p_build.add_argument("--sha", required=True)
    p_build.add_argument("--out", type=Path, required=True)
    p_build.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    p_publish = sub.add_parser("publish")
    p_publish.add_argument("--repo", required=True)
    p_publish.add_argument("--map", type=Path, required=True)
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--repo", required=True)
    p_fetch.add_argument("--dest", type=Path, required=True)
    p_ledger = sub.add_parser("ledger")
    p_ledger.add_argument("--repo", required=True)
    p_ledger.add_argument("--out", type=Path, required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / SEED)
    p_run.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    p_run.add_argument("--gates", nargs="*", default=[], choices=sorted(GATES), help="named gates to footprint too")
    args = parser.parse_args(argv)
    when = now or datetime.now(UTC)
    try:
        if args.command == "build":
            footprints = build(args.dir, args.sha, args.repo_root, when)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(render(footprints), encoding="utf-8", newline="\n")
            print(
                f"[e2e-footprints] wrote {args.out}: {len(footprints['specs'])} specs, {len(footprints['paths'])} paths"
            )
        elif args.command == "publish":
            print(publish(args.repo, args.map, send))
        elif args.command == "fetch":
            print(fetch(args.repo, args.dest, _bounded_gh if api is _gh else api))
        elif args.command == "ledger":
            print(ledger(args.repo, args.out, when, api))
        else:
            print(run(args.repo_root, args.out, when, args.gates))
    except (MapError, OSError, ValueError, KeyError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"::error title=e2e footprint map::{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
