#!/usr/bin/env python3
"""Hourly error-sink triage: fingerprint runtime errors and file queue:ready issues.

Issue #2673. Reads JSONL deltas from nucbox and remote Mac hosts, groups by a
stable fingerprint (source_site + message class with numbers, ids and paths
masked), and opens or updates GitHub issues when thresholds fire.

Triage scope is runtime engine/client JSONL errors only. Build/CI rows
(``kind=build`` or ``source_site`` starting with ``build:``) stay in the JSONL
for grepping (ADR-0017 / OBS-01) but are excluded from flood filing (ADR-0047;
issues #2845, #2846).

MINI-PRD
    R1 Fixture dry-run ........................................... done + regression
       [if] a fixture sink has three fingerprints (one above threshold, one below,
            one already filed) and dry-run is on
            [then] exactly one gh issue-create and one gh issue-comment are printed
            [else stop]
    R2 KPI staleness ............................................. done + regression
       [if] the last successful run timestamp is older than two hours
            [then] kpi reports the sink-triage line as stale (health FAIL)
            [else stop]
    R3 Burst filing .............................................. done + regression
       [if] one fingerprint reaches 100 events in a single window on any host
            [then] a queue:ready issue is filed within the next hourly tick
            [else stop]
    R4 Per-host isolation (issue #3431) .......................... done + regression
       [if] a host is unreachable and another yields records [then] the rest are
            still triaged and the skip is named [else stop]

-Claude
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.shared.telemetry.error_id import classify_message

WINDOW_COUNT_THRESHOLD = 100
RUNS_MIN = 3
RUNS_SUM_THRESHOLD = 10
COMMENT_COOLDOWN_S = 3600
STALE_AFTER_S = 7200
TAIL_BYTES = 50 * 1024 * 1024
MARKER_PREFIX = "<!-- sink-fingerprint:"
REPO_DEFAULT = "maintainer/music-dj-tools"

_PATH_RE = re.compile(r"(?:/[\w.@+-]+)+|~(?:/[\w.@+-]+)+|[A-Za-z]:\\(?:[\\ \w.@+-]+)+")


@dataclass(frozen=True)
class HostSource:
    name: str
    mode: str
    sink_path: str
    truncations_path: str | None = None


@dataclass(frozen=True)
class SourceSkip:
    source: str
    mode: str
    path: str
    error: str


@dataclass
class SinkRecord:
    host: str
    source_site: str
    message: str
    build_sha: str
    error_id: str
    raw: dict[str, Any]


@dataclass
class FingerprintStats:
    fingerprint: str
    count: int = 0
    hosts: set[str] = field(default_factory=set)
    build_shas: set[str] = field(default_factory=set)
    examples: list[dict[str, Any]] = field(default_factory=list)

    def add(self, record: SinkRecord) -> None:
        self.count += 1
        self.hosts.add(record.host)
        if record.build_sha and record.build_sha != "unknown":
            self.build_shas.add(record.build_sha)
        if len(self.examples) < 2:
            self.examples.append(record.raw)


@dataclass
class TriageState:
    offsets: dict[str, int]
    run_history: dict[str, list[int]]
    last_comment: dict[str, str]
    last_run: str | None = None

    @classmethod
    def load(cls, path: Path) -> TriageState:
        if not path.is_file():
            return cls(offsets={}, run_history={}, last_comment={})
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            offsets={str(k): int(v) for k, v in (data.get("offsets") or {}).items()},
            run_history={
                str(k): [int(x) for x in v] for k, v in (data.get("run_history") or {}).items()
            },
            last_comment={str(k): str(v) for k, v in (data.get("last_comment") or {}).items()},
            last_run=data.get("last_run"),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "offsets": self.offsets,
            "run_history": self.run_history,
            "last_comment": self.last_comment,
            "last_run": self.last_run,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class AllSourcesUnreachable(RuntimeError):
    """Carries the per-host skips when no configured source yielded a path."""

    def __init__(self, detail: str, skips: list[SourceSkip]) -> None:
        super().__init__(f"every source failed: {detail}")
        self.skips = skips


@dataclass
class RunResult:
    new_issues: int = 0
    comments: int = 0
    fingerprints_seen: int = 0
    commands: list[str] = field(default_factory=list)
    skips: list[SourceSkip] = field(default_factory=list)


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def triage_fingerprint(source_site: str, message: str) -> str:
    site = source_site.strip().lower()
    klass = classify_message(message)
    klass = _PATH_RE.sub("<tok>", klass)
    digest = hashlib.sha256(f"{site}\n{klass}".encode()).hexdigest()
    return digest[:12]


def marker_for(fingerprint: str) -> str:
    return f"{MARKER_PREFIX} {fingerprint} -->"


def _should_triage_record(raw: dict[str, Any]) -> bool:
    """Return True for runtime rows; skip OBS-01 kind=build / build:* CI telemetry."""
    kind = str(raw.get("kind") or "").strip().lower()
    if kind == "build":
        return False
    source_site = str(raw.get("source_site") or "").strip().lower()
    if source_site.startswith("build:"):
        return False
    return True


def _triage_runtime_record(record: SinkRecord) -> bool:
    """Filter records_in paths that bypass parse_record (tests, direct injection)."""
    return _should_triage_record(record.raw)


def parse_record(line: str, host_label: str) -> SinkRecord | None:
    line = line.strip()
    if not line:
        return None
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    message = str(raw.get("message") or "").strip()
    source_site = str(raw.get("source_site") or "").strip()
    if not message or not source_site:
        return None
    if not _should_triage_record(raw):
        return None
    return SinkRecord(
        host=str(raw.get("host") or host_label),
        source_site=source_site,
        message=message,
        build_sha=str(raw.get("build_sha") or "unknown"),
        error_id=str(raw.get("error_id") or ""),
        raw=raw,
    )


def aggregate(records: list[SinkRecord]) -> dict[str, FingerprintStats]:
    out: dict[str, FingerprintStats] = {}
    for record in records:
        if not _triage_runtime_record(record):
            continue
        fp = triage_fingerprint(record.source_site, record.message)
        if fp not in out:
            out[fp] = FingerprintStats(fingerprint=fp)
        out[fp].add(record)
    return out


def threshold_met(stats: FingerprintStats, prior_runs: list[int]) -> bool:
    if stats.count >= WINDOW_COUNT_THRESHOLD:
        return True
    recent = (prior_runs + [stats.count])[-RUNS_MIN:]
    if len(recent) < RUNS_MIN:
        return False
    return sum(recent) >= RUNS_SUM_THRESHOLD


def read_delta_bytes(path: Path, offset: int, tail_bytes: int) -> tuple[bytes, int]:
    if not path.is_file():
        return b"", offset
    size = path.stat().st_size
    if size <= offset:
        return b"", size
    start = offset
    if size - start > tail_bytes:
        start = size - tail_bytes
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read()
    return data, size


def ssh_fetch(host: str, remote_path: str, offset: int, tail_bytes: int) -> tuple[bytes, int]:
    quoted = remote_path.replace("'", "'\\''")
    size_cmd = ["ssh", "-o", "BatchMode=yes", host, f"wc -c < '{quoted}' 2>/dev/null || echo 0"]
    size_out = subprocess.check_output(size_cmd, text=True).strip()
    try:
        size = int(size_out.split()[0])
    except (ValueError, IndexError):
        return b"", offset
    if size <= offset:
        return b"", size
    start = offset
    if size - start > tail_bytes:
        start = size - tail_bytes
    read_cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        host,
        f"tail -c +{start + 1} '{quoted}' 2>/dev/null",
    ]
    data = subprocess.check_output(read_cmd)
    return data, size


def collect_host_records(
    source: HostSource,
    state: TriageState,
    fetch_local: Callable[[Path, int, int], tuple[bytes, int]] | None = None,
    fetch_ssh: Callable[[str, str, int, int], tuple[bytes, int]] | None = None,
) -> tuple[list[SinkRecord], SourceSkip | None, bool]:
    """Read one source's paths, isolating a host that cannot be reached.

    An unreachable host makes ssh itself exit 255 before the remote-side
    ``|| echo 0`` in ``ssh_fetch`` can run, so the failure is scoped to THIS
    source instead of aborting the lane (issue #3431). The failing path's offset
    is left unwritten -- nothing was consumed, so the next run resumes there --
    and the source's second path is not retried, so at most one skip. The
    trailing flag reports whether any path was read at all, which is what
    separates a partial run from a run that reached no host.
    """
    fetch_local = fetch_local or read_delta_bytes
    fetch_ssh = fetch_ssh or ssh_fetch
    records: list[SinkRecord] = []
    reached = False
    for path_str in (source.sink_path, source.truncations_path):
        if not path_str:
            continue
        key = f"{source.name}:{path_str}"
        offset = state.offsets.get(key, 0)
        try:
            if source.mode == "local":
                data, new_offset = fetch_local(Path(path_str), offset, TAIL_BYTES)
            else:
                data, new_offset = fetch_ssh(source.name, path_str, offset, TAIL_BYTES)
        except (OSError, subprocess.SubprocessError) as exc:
            return records, SourceSkip(source.name, source.mode, path_str, str(exc)), reached
        state.offsets[key] = new_offset
        reached = True
        text = data.decode("utf-8", errors="replace")
        for line in text.splitlines():
            rec = parse_record(line, source.name)
            if rec is not None:
                records.append(rec)
    return records, None, reached


def find_open_issue(
    repo: str,
    fingerprint: str,
    gh_run: Callable[..., subprocess.CompletedProcess],
) -> int | None:
    marker = marker_for(fingerprint)
    proc = gh_run(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--search",
            f"in:body {marker}",
            "--json",
            "number",
            "--jq",
            ".[0].number // empty",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh issue list failed: {proc.stderr.strip()}")
    out = proc.stdout.strip()
    return int(out) if out.isdigit() else None


def draft_acceptance(
    stats: FingerprintStats,
    sb_run: Callable[[list[str]], str] | None,
) -> str:
    if sb_run is None:
        return (
            "- [ ] Stop the runtime error from recurring on every host in the count table\n"
            "- [ ] Add a regression test that would fail if the error returns\n"
        )
    prompt = (
        "Draft 2-3 acceptance criteria bullets for a GitHub issue about this runtime error. "
        "Reply with ONLY markdown checklist lines (- [ ] ...), no prose.\n\n"
        f"fingerprint={stats.fingerprint}\n"
        f"hosts={sorted(stats.hosts)}\n"
        f"count={stats.count}\n"
        f"build_shas={sorted(stats.build_shas)}\n"
        f"examples={json.dumps(stats.examples, ensure_ascii=False)[:4000]}\n"
    )
    text = sb_run(
        [
            "run",
            "--provider",
            "cursor",
            "--model",
            "composer-2.5",
            "--access",
            "read",
            "--timeout-s",
            "300",
            "--prompt",
            prompt,
        ]
    )
    lines = [ln for ln in text.splitlines() if ln.strip().startswith("- [")]
    if not lines:
        raise RuntimeError("acceptance draft returned no checklist lines")
    return "\n".join(lines) + "\n"


def issue_body(stats: FingerprintStats, acceptance: str) -> str:
    hosts = ", ".join(sorted(stats.hosts)) or "unknown"
    shas = ", ".join(sorted(stats.build_shas)) or "unknown"
    examples = "\n\n".join(
        f"```json\n{json.dumps(ex, indent=2, sort_keys=True)}\n```" for ex in stats.examples
    )
    return (
        f"Auto-filed by sink-triage (issue #2673) at {utc_now_iso()}.\n\n"
        f"{marker_for(stats.fingerprint)}\n\n"
        f"count={stats.count}\n"
        f"hosts={hosts}\n"
        f"build_sha={shas}\n\n"
        f"## Examples\n{examples}\n\n"
        f"## Acceptance criteria\n{acceptance}\n"
    )


def comment_body(stats: FingerprintStats) -> str:
    hosts = ", ".join(sorted(stats.hosts)) or "unknown"
    return (
        f"sink-triage update at {utc_now_iso()}: count={stats.count} hosts={hosts} "
        f"build_sha={', '.join(sorted(stats.build_shas)) or 'unknown'}"
    )


def comment_allowed(state: TriageState, fingerprint: str, now: datetime) -> bool:
    last = state.last_comment.get(fingerprint)
    if not last:
        return True
    try:
        prev = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now - prev).total_seconds() >= COMMENT_COOLDOWN_S


def run_triage(
    *,
    sources: list[HostSource],
    state_path: Path,
    kpi_path: Path,
    repo: str,
    dry_run: bool,
    now: datetime | None = None,
    gh_run: Callable[..., subprocess.CompletedProcess] | None = None,
    sb_run: Callable[[list[str]], str] | None = None,
    fetch_local: Callable[[Path, int, int], tuple[bytes, int]] | None = None,
    fetch_ssh: Callable[[str, str, int, int], tuple[bytes, int]] | None = None,
    records_in: list[SinkRecord] | None = None,
) -> RunResult:
    now = now or datetime.now(UTC)
    state = TriageState.load(state_path)
    result = RunResult()
    records = records_in if records_in is not None else []
    if records_in is None:
        reached_any = False
        for source in sources:
            got, skip, reached = collect_host_records(source, state, fetch_local, fetch_ssh)
            records.extend(got)
            reached_any = reached_any or reached
            if skip is not None:
                result.skips.append(skip)
        if sources and not reached_any:
            detail = "; ".join(f"{s.source} {s.path}: {s.error}" for s in result.skips)
            raise AllSourcesUnreachable(detail, result.skips)
    grouped = aggregate(records)
    result.fingerprints_seen = len(grouped)

    def _gh(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if gh_run is not None:
            return gh_run(args, **kwargs)
        return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)

    for fp, stats in sorted(grouped.items()):
        history = state.run_history.get(fp, [])
        if not threshold_met(stats, history):
            state.run_history[fp] = (history + [stats.count])[-RUNS_MIN:]
            continue
        existing = find_open_issue(repo, fp, _gh)
        if existing is None:
            acceptance = draft_acceptance(stats, sb_run)
            title = f"runtime error flood: {fp} ({stats.count} in window)"
            body = issue_body(stats, acceptance)
            cmd = (
                f"gh issue create --repo {repo} --title '{title}' "
                f"--label bug --label queue:p1 --label queue:ready --body $'...'"
            )
            if dry_run:
                result.commands.append(cmd)
            else:
                argv = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
                argv += ["--label", "bug", "--label", "queue:p1", "--label", "queue:ready"]
                proc = _gh(argv)
                if proc.returncode != 0:
                    raise RuntimeError(f"gh issue create failed: {proc.stderr.strip()}")
            result.new_issues += 1
        elif comment_allowed(state, fp, now):
            body = comment_body(stats)
            cmd = f"gh issue comment {existing} --repo {repo} --body '{body}'"
            if dry_run:
                result.commands.append(cmd)
            else:
                proc = _gh(["issue", "comment", str(existing), "--repo", repo, "--body", body])
                if proc.returncode != 0:
                    raise RuntimeError(f"gh issue comment failed: {proc.stderr.strip()}")
            state.last_comment[fp] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            result.comments += 1
        state.run_history[fp] = (history + [stats.count])[-RUNS_MIN:]

    state.last_run = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not dry_run:
        state.save(state_path)
        kpi_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_run": state.last_run,
            "new_issues": result.new_issues,
            "fingerprints": result.fingerprints_seen,
            "skipped_sources": [asdict(skip) for skip in result.skips],
        }
        kpi_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return result


def default_sources(jobs_dir: Path) -> list[HostSource]:
    return [
        HostSource(
            name="nucbox",
            mode="local",
            sink_path=str(jobs_dir / "logs/opendj-error-sink.jsonl"),
            truncations_path=str(Path.home() / ".cache/opendj-host-disk-mac/truncations.jsonl"),
        ),
        HostSource(
            name="silver",
            mode="ssh",
            sink_path="~/Library/Logs/opendj/error-sink.jsonl",
            truncations_path="~/.cache/opendj-host-disk-mac/truncations.jsonl",
        ),
        HostSource(
            name="air",
            mode="ssh",
            sink_path="~/Library/Logs/opendj/error-sink.jsonl",
            truncations_path="~/.cache/opendj-host-disk-mac/truncations.jsonl",
        ),
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Error-sink triage lane (issue #2673)")
    parser.add_argument("--dry-run", action="store_true", help="Print gh commands only")
    parser.add_argument(
        "--jobs-dir", type=Path, default=Path(os.environ.get("JOBS_DIR", Path.home() / "jobs"))
    )
    parser.add_argument("--repo", default=os.environ.get("SINK_TRIAGE_REPO", REPO_DEFAULT))
    return parser.parse_args(argv)


def _append_log(log_path: Path, text: str) -> None:
    prior = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
    log_path.write_text(prior + text, encoding="utf-8")


def _report_error(log_path: Path, exc: Exception) -> int:
    line = f"{utc_now_iso()} sink-triage ERROR {exc}\n"
    _append_log(log_path, line)
    print(line, file=sys.stderr)
    return 1


def _log_skips(log_path: Path, skips: list[SourceSkip]) -> None:
    """Name every unreachable host, on a continued run or an aborted one."""
    for skip in skips:
        head = f"{utc_now_iso()} sink-triage SKIPPED host={skip.source} mode={skip.mode} "
        _append_log(log_path, f"{head}path={skip.path} error={skip.error}\n")


def main(
    argv: list[str] | None = None,
    *,
    fetch_local: Callable[[Path, int, int], tuple[bytes, int]] | None = None,
    fetch_ssh: Callable[[str, str, int, int], tuple[bytes, int]] | None = None,
) -> int:
    args = parse_args(argv)
    state_path = args.jobs_dir / "state/sink-triage.json"
    kpi_path = args.jobs_dir / "state/sink-triage-kpi.json"
    log_path = args.jobs_dir / "logs/sink-triage.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    sources = default_sources(args.jobs_dir)
    try:
        result = run_triage(
            sources=sources,
            state_path=state_path,
            kpi_path=kpi_path,
            repo=args.repo,
            dry_run=args.dry_run,
            fetch_local=fetch_local,
            fetch_ssh=fetch_ssh,
        )
    except AllSourcesUnreachable as exc:
        _log_skips(log_path, exc.skips)
        return _report_error(log_path, exc)
    except Exception as exc:
        return _report_error(log_path, exc)
    _log_skips(log_path, result.skips)
    summary = (
        f"{utc_now_iso()} sink-triage new_issues={result.new_issues} "
        f"comments={result.comments} fingerprints={result.fingerprints_seen} "
        f"dry_run={args.dry_run} skipped={len(result.skips)}/{len(sources)}\n"
    )
    _append_log(log_path, summary)
    for cmd in result.commands:
        print(cmd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
