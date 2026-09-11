"""Shared primitives for the ci-fixer lane: dedupe ledger, known-unfixable
detection, guard-rail path checks, and PR comment rendering.

Split from scripts/ci_fixer.py so reasoning (here) and gh/codex orchestration
(ci_fixer.py) stay separately testable, matching the ci_health_core /
trunk_job_verdict_core split: dependency runs one way, ci_fixer_core <- ci_fixer.

THIS LANE PROPOSES, IT NEVER APPLIES: nothing here or in the caller runs
`git push`, edits a workflow file, or touches a gate/ratchet/budget config --
the guard-rail check below makes that a checked property of every diff
considered for posting, not just a convention someone remembers.

KNOWN-UNFIXABLE, NOT GUESSED-FIXABLE: #1029 is the live case. The OSS history
rewrite (#911) orphaned every provenance SHA `data/progress-tree.yaml` cites,
so `test_seed_statuses_reflect_reality` fails on every fresh clone until the
filter-repo commit map (Air-only) re-points them. Inventing a plausible SHA
to "fix" it would fabricate ledger provenance -- worse than the failure it
replaces. `classify_known_unfixable` matches #1029's own failure SIGNATURE so
the caller declines instead of spawning a worker at all.

-Claude
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# ----- configuration ---------------------------------------------------------------

REPO = "maintainer/music-dj-tools"

# Self-hosted Linux runners actually taking traffic (#1017). The 13 nucbox runners
# lack corepack/python -- every job routed there is an environment gap, not a code
# failure this lane could fix with a diff.
LIVE_RUNNERS = ("agentbox", "agentbox-2", "agentbox-3")

# `agentbox` holds a pre-rewrite clone, so a green fast lane there can be a FALSE
# green (it still has the objects that make #1029's orphaned SHAs resolve). Every
# verdict records runner_name so a reader can see when "passed" means "on the stale clone".
SUSPECT_FALSE_GREEN_RUNNERS = ("agentbox",)

MAX_CONCURRENT_FIXERS = 2
WORKER_TIMEOUT_SECONDS = 20 * 60
RECHECK_TIMEOUT_SECONDS = 20 * 60
CLAIM_LEASE_SECONDS = WORKER_TIMEOUT_SECONDS + RECHECK_TIMEOUT_SECONDS + 60

LEDGER_PATH = Path(".planning/ci-fixer/ledger.json")
KPI_EVENTS_PATH = Path(".planning/ci-fixer/kpi-events.jsonl")

CI_FIX_LABEL = "ci-fix:proposed"

# Paths a proposed diff must never touch (prefix-matched against each diff header's
# `a/...` path). Touching any of these needs a human decision, not a green CI run.
GUARDED_PATH_PREFIXES: tuple[str, ...] = (
    ".github/workflows/",
    "ops/quality/baseline.json",
    ".importlinter",
    "scripts/bench/kpi_ledger.json",
    "docs/perf/kpi-ledger.json",
    "data/progress-tree.yaml",
)

# Filename fragments marking a gate/ratchet/budget config not caught by the prefix
# list above (new ratchet files, per-app baselines).
GUARDED_NAME_FRAGMENTS: tuple[str, ...] = ("ratchet", "baseline", "budget")

# The exact failure signatures #1029's own assertions raise (trunk_job_verdict_core's
# PreconditionError text, or test_progress.py's "seed sha broken:" / "cites
# unresolvable sha"). Matching strings, not test names, so a new test hitting the
# same defect is still recognized without a code change here.
KNOWN_UNFIXABLE_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("orphaned-provenance-sha", "is not present in this clone"),
    ("orphaned-provenance-sha", "seed sha broken:"),
    ("orphaned-provenance-sha", "cites unresolvable sha"),
)

KNOWN_UNFIXABLE_EXPLANATION = {
    "orphaned-provenance-sha": (
        "Refs #1029: the OSS history rewrite (#911) orphaned every pre-rewrite "
        "provenance SHA this repo commits as data. Re-pointing them needs the "
        "filter-repo commit-map, which lives only on the Air, not this clone. "
        "A fix here would have to invent a SHA, which fabricates ledger "
        "provenance -- declining rather than guessing."
    ),
}


class PreconditionError(RuntimeError):
    """Raised when the lane cannot trust its own inputs. Never swallowed."""


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ----- data --------------------------------------------------------------------------


@dataclass(frozen=True)
class FailedJob:
    """One failed job on one open PR's self-hosted CI run, reduced to what the lane
    reasons about."""

    run_id: int
    job_id: int
    job_name: str
    pr_number: int
    head_sha: str
    runner_name: str
    workflow: str
    html_url: str
    log_excerpt: str
    created_at: str = ""

    @property
    def dedupe_key(self) -> str:
        # run_id ALONE (#1017: "one fixer per failed run, deduped by run id") --
        # a run with several failing jobs still gets exactly one proposal attempt.
        return str(self.run_id)

    @property
    def is_suspect_runner(self) -> bool:
        return self.runner_name in SUSPECT_FALSE_GREEN_RUNNERS


@dataclass(frozen=True)
class FixOutcome:
    """What the lane decided to do about one FailedJob, and what it posted."""

    job: FailedJob
    disposition: str  # "known-unfixable" | "fix-proposed" | "diagnosis-only"
    reason: str
    diff_text: str = ""
    comment_body: str = ""
    decided_at: str = field(default_factory=_utc_now_iso)


# ----- known-unfixable detection ------------------------------------------------------


def classify_known_unfixable(log_excerpt: str) -> str | None:
    """Reason key if `log_excerpt` matches a known-unfixable signature, else None.

    A DECLINE list, not a fix list: a match means the correct action is outside
    what a local diff can do, so the lane must not attempt one at all.
    """
    for reason, signature in KNOWN_UNFIXABLE_SIGNATURES:
        if signature in log_excerpt:
            return reason
    return None


# ----- guard rails ---------------------------------------------------------------------

def _is_guarded_path(path: str) -> bool:
    if any(path.startswith(prefix) for prefix in GUARDED_PATH_PREFIXES):
        return True
    name = path.rsplit("/", 1)[-1].lower()
    return any(fragment in name for fragment in GUARDED_NAME_FRAGMENTS)


def diff_touches_guarded_paths(diff_text: str) -> list[str]:
    """Every path in `diff_text` this lane must never propose touching, else [].

    A non-empty result means the caller must refuse to post the diff and fall
    back to a diagnosis-only comment -- enforced here once, not per call site.
    """
    touched: set[str] = set()
    for line in diff_text.splitlines():
        if not line.startswith("diff --git "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            return ["<unparseable diff header>"]
        if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
            return ["<unparseable diff header>"]
        for raw_path in parts[2:]:
            if not raw_path.startswith(("a/", "b/")):
                return ["<unparseable diff header>"]
            path = raw_path[2:]
            if _is_guarded_path(path):
                touched.add(path)
    return sorted(touched)


# ----- ledger (dedupe + concurrency) ----------------------------------------------------


def load_ledger(path: Path | None = None) -> dict:
    if path is None:
        path = LEDGER_PATH
    if not path.exists():
        return {"entries": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PreconditionError(f"ledger {path} is corrupt JSON: {exc}") from exc
    if not isinstance(payload, dict) or "entries" not in payload:
        raise PreconditionError(f"ledger {path} has no 'entries' key")
    return payload


def save_ledger(ledger: dict, path: Path | None = None) -> None:
    if path is None:
        path = LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(ledger, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def ledger_transaction(path: Path | None = None):
    """Serialize one load-check-update-save cycle for every polling process."""
    if path is None:
        path = LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            ledger = load_ledger(path)
            yield ledger
            save_ledger(ledger, path)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def already_seen(ledger: dict, job: FailedJob) -> bool:
    return job.dedupe_key in ledger["entries"]


def record_outcome(ledger: dict, outcome: FixOutcome, status: str = "done") -> dict:
    """Record a FixOutcome in the ledger, returning the updated ledger.

    `status` is "in_progress" while a worker runs (for concurrency counting),
    "done" once a disposition and comment are settled.
    """
    ledger["entries"][outcome.job.dedupe_key] = {
        "run_id": outcome.job.run_id,
        "job_id": outcome.job.job_id,
        "pr_number": outcome.job.pr_number,
        "job_name": outcome.job.job_name,
        "runner_name": outcome.job.runner_name,
        "disposition": outcome.disposition,
        "reason": outcome.reason,
        "status": status,
        "decided_at": outcome.decided_at,
    }
    return ledger


def mark_in_progress(
    ledger: dict,
    job: FailedJob,
    now_epoch: float | None = None,
    lease_seconds: int = CLAIM_LEASE_SECONDS,
) -> dict:
    if now_epoch is None:
        now_epoch = time.time()
    ledger["entries"][job.dedupe_key] = {
        "run_id": job.run_id,
        "job_id": job.job_id,
        "pr_number": job.pr_number,
        "job_name": job.job_name,
        "runner_name": job.runner_name,
        "disposition": "",
        "reason": "",
        "status": "in_progress",
        "decided_at": _utc_now_iso(),
        "lease_expires_at": now_epoch + lease_seconds,
    }
    return ledger


def count_in_progress(ledger: dict) -> int:
    return sum(1 for entry in ledger["entries"].values() if entry.get("status") == "in_progress")


def concurrency_available(ledger: dict, cap: int = MAX_CONCURRENT_FIXERS) -> int:
    """How many more fixers may start right now, never negative."""
    return max(0, cap - count_in_progress(ledger))


def release_expired_claims(ledger: dict, now_epoch: float | None = None) -> list[str]:
    """Remove worker claims that died before reporting a terminal outcome."""
    if now_epoch is None:
        now_epoch = time.time()
    released = []
    for key, entry in list(ledger["entries"].items()):
        lease_expires_at = entry.get("lease_expires_at")
        if entry.get("status") != "in_progress":
            continue
        if not isinstance(lease_expires_at, (int, float)):
            raise PreconditionError(f"ledger claim {key} has no numeric lease expiry")
        if lease_expires_at <= now_epoch:
            del ledger["entries"][key]
            released.append(key)
    return released


def claim_job(job: FailedJob, path: Path | None = None) -> str:
    """Atomically claim one run, returning claimed, seen, or capacity-reached."""
    with ledger_transaction(path) as ledger:
        release_expired_claims(ledger)
        if already_seen(ledger, job):
            return "seen"
        if concurrency_available(ledger) <= 0:
            return "capacity-reached"
        mark_in_progress(ledger, job)
        return "claimed"


def release_claim(job: FailedJob, path: Path | None = None) -> None:
    """Make an interrupted or unposted outcome retryable on the next poll."""
    with ledger_transaction(path) as ledger:
        entry = ledger["entries"].get(job.dedupe_key)
        if entry is not None and entry.get("status") == "in_progress":
            del ledger["entries"][job.dedupe_key]


def finalize_outcome(outcome: FixOutcome, path: Path | None = None) -> None:
    """Mark a claim terminal only after its GitHub disposition was posted."""
    with ledger_transaction(path) as ledger:
        entry = ledger["entries"].get(outcome.job.dedupe_key)
        if entry is None or entry.get("status") != "in_progress":
            raise PreconditionError(
                f"cannot finalize run {outcome.job.run_id}: its active claim is missing"
            )
        record_outcome(ledger, outcome)


# ----- comment rendering ---------------------------------------------------------------


def outcome_marker(job: FailedJob) -> str:
    return f"<!-- ci-fixer-run:{job.run_id} -->"


def render_known_unfixable_comment(job: FailedJob, reason: str) -> str:
    explanation = KNOWN_UNFIXABLE_EXPLANATION.get(
        reason, "This failure matches a known-unfixable-by-this-lane signature."
    )
    return (
        f"{outcome_marker(job)}\n## ci-fixer: declined ({reason})\n\n"
        f"Job **{job.job_name}** failed on run [{job.run_id}]({job.html_url}) "
        f"(runner `{job.runner_name}`).\n\n"
        f"{explanation}\n\n"
        "No diff proposed. This is not a code fix; it needs a decision outside "
        "what a local diff can do.\n"
    )


def _suspect_runner_note(job: FailedJob, advice: str) -> str:
    if not job.is_suspect_runner:
        return "\n"
    return f"\n\n**Note:** this ran on `agentbox`, which holds a pre-rewrite clone -- {advice}\n"


def render_diagnosis_comment(job: FailedJob, root_cause: str) -> str:
    suspect = _suspect_runner_note(
        job, "a green result there can be a false green; re-check on agentbox-2/-3"
    )
    return (
        f"{outcome_marker(job)}\n## ci-fixer: diagnosis only\n\n"
        f"Job **{job.job_name}** failed on run [{job.run_id}]({job.html_url}) "
        f"(runner `{job.runner_name}`).\n\n"
        f"**Root cause:** {root_cause}\n\n"
        "Could not produce a fix that reproduces green locally within the "
        f"{WORKER_TIMEOUT_SECONDS // 60}-minute cap. No diff proposed."
        f"{suspect}"
    )


def render_fix_comment(job: FailedJob, root_cause: str, diff_text: str) -> str:
    suspect = _suspect_runner_note(job, "verify green on agentbox-2/-3 before applying")
    return (
        f"{outcome_marker(job)}\n## ci-fixer: fix proposed\n\n"
        f"Job **{job.job_name}** failed on run [{job.run_id}]({job.html_url}) "
        f"(runner `{job.runner_name}`).\n\n"
        f"**Root cause:** {root_cause}\n\n"
        "Reproduced green locally after this diff. Applied as a proposal only -- "
        "this lane does not push. Review, then:\n\n"
        "```\ngit apply <<'EOF'\n"
        f"{diff_text}\n"
        "EOF\n```\n"
        f"\n<details><summary>diff</summary>\n\n```diff\n{diff_text}\n```\n\n"
        f"</details>{suspect}"
    )


# ----- KPI events ------------------------------------------------------------------------


def append_kpi_event(outcome: FixOutcome, path: Path | None = None) -> None:
    """Idempotently append one JSONL record before terminal ledger persistence."""
    if path is None:
        path = KPI_EVENTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": outcome.decided_at,
        "run_id": outcome.job.run_id,
        "job_id": outcome.job.job_id,
        "pr_number": outcome.job.pr_number,
        "runner_name": outcome.job.runner_name,
        "disposition": outcome.disposition,
        "reason": outcome.reason,
    }
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        existing_run_ids = {
            json.loads(line).get("run_id") for line in handle if line.strip()
        }
        if outcome.job.run_id in existing_run_ids:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_kpi_events(path: Path | None = None) -> list[dict]:
    if path is None:
        path = KPI_EVENTS_PATH
    if not path.exists():
        return []
    events = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise PreconditionError(f"{path}:{line_no} is corrupt JSONL: {exc}") from exc
    return events


@dataclass(frozen=True)
class KpiReport:
    total_events: int
    fixes_proposed: int
    diagnosis_only: int
    known_unfixable: int

    @property
    def runner_environment_share(self) -> float | None:
        # Share of examined failures that were environment/history (known_unfixable,
        # e.g. #1029), not genuine code failures (fix-proposed/diagnosis-only).
        # None (not 0.0) when nothing has been examined yet.
        if self.total_events == 0:
            return None
        return self.known_unfixable / self.total_events


def build_kpi_report(events: list[dict]) -> KpiReport:
    proposed = sum(1 for e in events if e.get("disposition") == "fix-proposed")
    diagnosis = sum(1 for e in events if e.get("disposition") == "diagnosis-only")
    unfixable = sum(1 for e in events if e.get("disposition") == "known-unfixable")
    return KpiReport(
        total_events=len(events),
        fixes_proposed=proposed,
        diagnosis_only=diagnosis,
        known_unfixable=unfixable,
    )
