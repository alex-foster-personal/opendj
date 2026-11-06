"""Run the gitleaks PR scan and trufflehog full-history scan.

The shell entrypoint retains shell-owned checks, while this module owns the
portable control fixture and scanner orchestration used by CI.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from scripts.security import materialize, secscan

PASS = 0
FAIL = 1
UNKNOWN = 2

PATH_SUPPRESSIONS: tuple[tuple[str, str, str], ...] = (
    (
        r"^\.planning/bifrost2-handoff/sync-kit/[^/]+/skills/cloudflare/"
        r"references/hyperdrive/",
        "2026-12-13",
        "vendored Cloudflare Hyperdrive docs: postgres:// examples with placeholder host and password",
    ),
    (
        r"^\.pnpm-store/",
        "2026-12-13",
        "pnpm store committed by mistake, history only: third-party package files",
    ),
    (
        r"^apps/webui/frontend/tests/unit/progress-repo-base\.test\.mjs$",
        "2026-12-13",
        'code-comment example URL (x-access-token placeholder, host "host")',
    ),
    (
        r"^\.xdg-cache/",
        "2027-01-13",
        "corepack cache committed by mistake, history only: pnpm.mjs docs example of a "
        "proxy URL with a placeholder user and password",
    ),
)

VALUE_SUPPRESSIONS: tuple[tuple[str, str, str], ...] = (
    (
        r"^https?://opendj\.localhost:[0-9]+@attacker\.example$",
        "2027-01-13",
        'open-redirect test vector: userinfo trick, the "password" is a port number '
        "(opener scope, ADR, open-in-browser tests, commit messages)",
    ),
    (
        r"^https?://opendj\.localhost:[0-9]+@evil\.example\.com$",
        "2027-01-13",
        'open-redirect test vector in tests/webui/test_auth.py: the "password" is a port number',
    ),
    (
        r"^https://x-access-token:ghs_redacted@github\.com$",
        "2027-01-13",
        "redaction placeholder: git-remote-identity and enqueue-gate tests, .test_durations test ids",
    ),
    (
        r"^https://x-access-token:ghs_\.\.\.@github\.com$",
        "2027-01-13",
        "elided-token placeholder in commit messages (5e20b6de9, 040e7ccdd)",
    ),
)


@dataclass(frozen=True)
class ScanContext:
    mode: str
    repo_root: Path
    work_dir: Path
    bin_dir: Path
    summary_file: Path
    out: Path
    gitleaks_config: Path
    git: str


def _resolved_git() -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH")
    return git


def make_control_repo(dest: Path) -> Path:
    """Create one committed synthetic GitHub-PAT-shaped token under *dest*."""
    git = _resolved_git()
    dest.mkdir(parents=True)
    token = f"ghp_{secrets.token_hex(18)}"
    (dest / "deploy.env").write_text(f"GITHUB_TOKEN={token}\n", encoding="utf-8", newline="\n")
    subprocess.run([git, "init", "--quiet", str(dest)], check=True)
    subprocess.run([git, "-C", str(dest), "add", "deploy.env"], check=True)
    subprocess.run(
        [
            git,
            "-C",
            str(dest),
            "-c",
            "user.name=control",
            "-c",
            "user.email=control@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "control: synthetic token",
        ],
        check=True,
    )
    return dest


def run_gitleaks_git(
    *,
    gitleaks_cmd: list[str],
    repo: Path,
    config: Path,
    report: Path,
    log_opts: str | None,
) -> int:
    """Run gitleaks against *repo* and return its exit code."""
    report.parent.mkdir(parents=True, exist_ok=True)
    command = [
        *gitleaks_cmd,
        "git",
        "--no-banner",
        "--redact",
        "--config",
        str(config),
        "--report-format",
        "json",
        "--report-path",
        str(report),
        "--exit-code",
        "1",
    ]
    if log_opts is not None:
        command.append(f"--log-opts={log_opts}")
    command.append(str(repo))
    return subprocess.run(command, check=False).returncode


def gitleaks_findings(report: Path) -> list[str]:
    """Return the non-secret finding lines emitted by secscan's gitleaks parser."""
    doc = json.loads(report.read_text(encoding="utf-8"))
    if not isinstance(doc, list):
        raise TypeError("gitleaks JSON root is not a list")
    return [
        f"{finding['RuleID']} {finding['File']}:{finding['StartLine']} commit={str(finding.get('Commit', ''))[:9]}"
        for finding in doc
    ]


def scan_git_for_secrets(
    repo: Path,
    *,
    gitleaks_cmd: list[str],
    config: Path,
    report: Path,
) -> list[str]:
    """Run gitleaks over a git fixture and return its parsed findings."""
    rc = run_gitleaks_git(
        gitleaks_cmd=gitleaks_cmd,
        repo=repo,
        config=config,
        report=report,
        log_opts=None,
    )
    if rc not in (PASS, FAIL):
        raise RuntimeError(f"gitleaks scan errored (exit {rc})")
    return gitleaks_findings(report)


def _context(mode: str) -> ScanContext:
    required = (
        "SECURITY_REPO_ROOT",
        "SECURITY_WORK_DIR",
        "SECURITY_BIN_DIR",
        "SECURITY_SUMMARY_FILE",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"missing environment: {', '.join(missing)}")
    repo_root = Path(os.environ["SECURITY_REPO_ROOT"]).resolve()
    work_dir = Path(os.environ["SECURITY_WORK_DIR"]).resolve()
    return ScanContext(
        mode=mode,
        repo_root=repo_root,
        work_dir=work_dir,
        bin_dir=Path(os.environ["SECURITY_BIN_DIR"]).resolve(),
        summary_file=Path(os.environ["SECURITY_SUMMARY_FILE"]).resolve(),
        out=work_dir / "secrets",
        gitleaks_config=repo_root / ".gitleaks.toml",
        git=_resolved_git(),
    )


def _row(
    ctx: ScanContext,
    scanner: str,
    control: str,
    findings: int | str,
    status: str,
    detail: str,
) -> None:
    ctx.summary_file.parent.mkdir(parents=True, exist_ok=True)
    with ctx.summary_file.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{scanner}\t{ctx.mode}\t{control}\t{findings}\t{status}\t{detail}\n")


def _unknown(ctx: ScanContext, scanner: str, message: str) -> int:
    print(f"UNKNOWN: {scanner} {message}", file=sys.stderr)
    _row(ctx, scanner, "?", "?", "UNKNOWN", message)
    return UNKNOWN


def _scanner_command(ctx: ScanContext, name: str) -> list[str] | None:
    candidates = (ctx.bin_dir / name, ctx.bin_dir / f"{name}.exe")
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate.resolve())]
    return None


def _git_output(ctx: ScanContext, *args: str) -> str:
    return subprocess.run(
        [ctx.git, "-C", str(ctx.repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _base_and_head(ctx: ScanContext) -> tuple[str, str]:
    head = _git_output(ctx, "rev-parse", os.environ.get("SECURITY_HEAD_SHA") or "HEAD")
    base_ref = os.environ.get("SECURITY_BASE_SHA") or os.environ.get("SECURITY_BASE_REF", "origin/main")
    base = _git_output(ctx, "merge-base", base_ref, head)
    return base, head


def _gitleaks_summary(
    report: Path,
    count_file: Path,
    *,
    report_md: Path | None = None,
    title: str = "gitleaks",
    quiet: bool = False,
) -> tuple[int, int]:
    args = argparse.Namespace(
        json=str(report),
        count_file=str(count_file),
        report_md=str(report_md) if report_md else None,
        title=title,
    )
    output = io.StringIO()
    with redirect_stdout(output):
        rc = secscan.cmd_gitleaks_summary(args)
    if not quiet:
        print(output.getvalue(), end="")
    count = int(count_file.read_text(encoding="utf-8").strip()) if rc == PASS else 0
    return rc, count


def _trufflehog_summary(
    report: Path,
    count_file: Path,
    *,
    suppress_raw: Path | None = None,
    report_md: Path | None = None,
    title: str = "trufflehog",
) -> tuple[int, int, str]:
    args = argparse.Namespace(
        jsonl=str(report),
        suppress_raw=str(suppress_raw) if suppress_raw else None,
        count_file=str(count_file),
        report_md=str(report_md) if report_md else None,
        title=title,
    )
    output = io.StringIO()
    with redirect_stdout(output):
        rc = secscan.cmd_trufflehog_summary(args)
    text = output.getvalue()
    count = int(count_file.read_text(encoding="utf-8").strip()) if rc == PASS else 0
    return rc, count, text


def _write_suppressions(path: Path, entries: tuple[tuple[str, str, str], ...]) -> str | None:
    today = datetime.now(UTC).date()
    regexes: list[str] = []
    for regex, expires, _reason in entries:
        expiry = date.fromisoformat(expires)
        if expiry <= today:
            return f"suppression expired {expires}: {regex}"
        regexes.append(regex)
    path.write_text("".join(f"{regex}\n" for regex in regexes), encoding="utf-8", newline="\n")
    return None


def _gitleaks_control(ctx: ScanContext, command: list[str]) -> tuple[int, int]:
    scanner = "gitleaks"
    control = make_control_repo(ctx.out / "control-repo")
    control_report = ctx.out / "control.json"
    rc = run_gitleaks_git(
        gitleaks_cmd=command,
        repo=control,
        config=ctx.gitleaks_config,
        report=control_report,
        log_opts=None,
    )
    if rc != FAIL:
        return _unknown(ctx, scanner, f"control did not fire (exit {rc}, expected 1)"), 0
    parsed, control_hits = _gitleaks_summary(control_report, ctx.out / "control.count", quiet=True)
    if parsed != PASS:
        return _unknown(ctx, scanner, "control report unparseable"), 0
    if control_hits == 0:
        return _unknown(ctx, scanner, "control did not fire (0 findings)"), 0
    return PASS, control_hits


def _scan_pr(ctx: ScanContext, command: list[str]) -> int:
    scanner = "gitleaks"
    control_status, control_hits = _gitleaks_control(ctx, command)
    if control_status != PASS:
        return control_status
    base, head = _base_and_head(ctx)
    commits = int(_git_output(ctx, "rev-list", "--count", f"{base}..{head}"))
    control_text = f"fired({control_hits})"
    short_range = f"{base[:9]}..{head[:9]}"
    if commits == 0:
        _row(ctx, scanner, control_text, 0, "SKIP", f"no commits in {short_range}")
        return PASS

    range_report = ctx.out / "range.json"
    rc = run_gitleaks_git(
        gitleaks_cmd=command,
        repo=ctx.repo_root,
        config=ctx.gitleaks_config,
        report=range_report,
        log_opts=f"{base}..{head}",
    )
    if rc not in (PASS, FAIL):
        return _unknown(ctx, scanner, f"range scan errored (exit {rc})")
    parsed, findings = _gitleaks_summary(
        range_report,
        ctx.out / "range.count",
        report_md=ctx.work_dir / "report.md",
        title=f"gitleaks: {short_range}",
    )
    if parsed != PASS:
        return _unknown(ctx, scanner, "range report unparseable")
    status = "PASS" if findings == 0 else "FAIL"
    _row(
        ctx,
        scanner,
        control_text,
        findings,
        status,
        f"{commits} commit(s) {short_range}",
    )
    return PASS if findings == 0 else FAIL


def scan_pr(ctx: ScanContext) -> int:
    scanner = "gitleaks"
    command = _scanner_command(ctx, scanner)
    if command is None:
        return _unknown(ctx, scanner, "binary missing")
    try:
        return _scan_pr(ctx, command)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return _unknown(ctx, scanner, f"scan orchestration errored: {exc}")


def _trufflehog_control(ctx: ScanContext, command: list[str]) -> tuple[int, int]:
    scanner = "trufflehog"
    control = make_control_repo(ctx.out / "control-repo")
    control_report = ctx.out / "control.jsonl"
    with control_report.open("w", encoding="utf-8", newline="\n") as handle:
        rc = subprocess.run(
            [
                *command,
                "git",
                control.resolve().as_uri(),
                "--no-verification",
                "--json",
                "--no-update",
            ],
            stdout=handle,
            check=False,
        ).returncode
    if rc != PASS:
        return _unknown(ctx, scanner, f"control scan errored (exit {rc})"), 0
    parsed, control_hits, _ = _trufflehog_summary(control_report, ctx.out / "control.count")
    if parsed != PASS:
        return _unknown(ctx, scanner, "control output unparseable"), 0
    if control_hits == 0:
        return _unknown(ctx, scanner, "control did not fire (0 detections)"), 0
    return PASS, control_hits


def _prepare_trufflehog_history(ctx: ScanContext) -> tuple[int, Path, Path]:
    scanner = "trufflehog"
    exclude_paths = ctx.out / "exclude-paths.txt"
    suppress_values = ctx.out / "suppress-values.txt"
    if materialize.materialize_repo(ctx.git, ctx.repo_root) != PASS:
        status = _unknown(
            ctx,
            scanner,
            "could not materialize git objects for file:// history scan (partial clone; set GH_TOKEN or GITHUB_TOKEN)",
        )
        return status, exclude_paths, suppress_values
    for path, entries in (
        (exclude_paths, PATH_SUPPRESSIONS),
        (suppress_values, VALUE_SUPPRESSIONS),
    ):
        expired = _write_suppressions(path, entries)
        if expired:
            return _unknown(ctx, scanner, expired), exclude_paths, suppress_values
    return PASS, exclude_paths, suppress_values


def _trufflehog_history(
    ctx: ScanContext,
    command: list[str],
    exclude_paths: Path,
    suppress_values: Path,
) -> tuple[int, int, str]:
    scanner = "trufflehog"
    history_report = ctx.out / "history.jsonl"
    with history_report.open("w", encoding="utf-8", newline="\n") as handle:
        rc = subprocess.run(
            [
                *command,
                "git",
                ctx.repo_root.as_uri(),
                "--results=verified,unknown",
                "--exclude-paths",
                str(exclude_paths),
                "--json",
                "--no-update",
            ],
            stdout=handle,
            check=False,
        ).returncode
    if rc != PASS:
        return _unknown(ctx, scanner, f"history scan errored (exit {rc})"), 0, ""
    parsed, findings, output = _trufflehog_summary(
        history_report,
        ctx.out / "history.count",
        suppress_raw=suppress_values,
        report_md=ctx.work_dir / "report.md",
        title="trufflehog: full history (verified + unverifiable)",
    )
    if parsed != PASS:
        return _unknown(ctx, scanner, "history output unparseable"), 0, ""
    return PASS, findings, output


def _record_full_result(ctx: ScanContext, control_hits: int, findings: int, output: str) -> int:
    scanner = "trufflehog"
    (ctx.out / "history.txt").write_text(output, encoding="utf-8", newline="\n")
    print(output, end="")
    control_text = f"fired({control_hits})"
    unverifiable = sum(" unverified " in line for line in output.splitlines())
    if findings == 0:
        _row(
            ctx,
            scanner,
            control_text,
            0,
            "PASS",
            "full history, verified live secrets only",
        )
        return PASS
    if unverifiable == findings:
        return _unknown(ctx, scanner, f"{findings} candidate(s) whose verification errored")
    _row(
        ctx,
        scanner,
        control_text,
        findings,
        "FAIL",
        "verified live secret(s) in history: rotate first",
    )
    return FAIL


def _scan_full(ctx: ScanContext, command: list[str]) -> int:
    status, control_hits = _trufflehog_control(ctx, command)
    if status != PASS:
        return status
    status, exclude_paths, suppress_values = _prepare_trufflehog_history(ctx)
    if status != PASS:
        return status
    status, findings, output = _trufflehog_history(ctx, command, exclude_paths, suppress_values)
    if status != PASS:
        return status
    return _record_full_result(ctx, control_hits, findings, output)


def scan_full(ctx: ScanContext) -> int:
    scanner = "trufflehog"
    command = _scanner_command(ctx, scanner)
    if command is None:
        return _unknown(ctx, scanner, "binary missing")
    try:
        return _scan_full(ctx, command)
    except (OSError, TypeError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return _unknown(ctx, scanner, f"scan orchestration errored: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("pr", "full"))
    args = parser.parse_args(argv)
    try:
        ctx = _context(args.mode)
    except (OSError, RuntimeError) as exc:
        print(f"UNKNOWN: secrets {exc}", file=sys.stderr)
        return UNKNOWN
    ctx.out.mkdir(parents=True, exist_ok=True)
    return scan_pr(ctx) if args.mode == "pr" else scan_full(ctx)


if __name__ == "__main__":
    raise SystemExit(main())
