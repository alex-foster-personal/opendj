"""Parse scanner JSON for scripts/security/*.sh. Stdlib only, Python 3.11+.

Every subcommand prints human-readable detail lines to stdout, appends a markdown
section to --report-md, and writes the in-scope finding count to --count-file.
Secret values are never read into output: gitleaks/trufflehog summaries print rule
or detector ids, file paths, lines and short commit ids only.

Exit codes: 0 = parsed (findings may be > 0), 2 = UNKNOWN (input could not be
measured: missing file, malformed JSON, expected subject absent, control silent).

Acceptance (one assertion each, exercised by `just security-scan`):
- [if] osv JSON lacks one of the expected manifests [then] exit 2 (UNKNOWN).
- [if] an IgnoredVulns entry lacks reason/ignoreUntil or expires > max days out [then] exit 2.
- [if] a head finding shares (ecosystem, package, id) with base [then] it is not NEW.
- [if] a semgrep control run lacks a required rule id [then] exit 2.
- [if] semgrep-diff-scope cannot run git or semgrep on changed paths [then] exit 2.
- [if] every changed path is on semgrep's default ignore list (tests/) [then] semgrep-diff-scope
  counts 0, so scan_sast.sh writes SKIP instead of running a scan that sees nothing.
- [if] a changed path is outside the default ignore list [then] semgrep-diff-scope counts it,
  even when an ignored path changed in the same diff.
- [if] semgrep-summary has --expected-scannable > 0 but loaded 0 rules [then] exit 2 (UNKNOWN).
- [if] semgrep-summary has --expected-scannable > 0, rules loaded, scanned 0 files, no errors,
  no results, and no --expect-file path missing [then] exit 0 (baseline excluded unchanged
  files; no new findings).
- [if] semgrep-summary has --expected-scannable > 0, scanned 0 files, and errors or results
  [then] exit 2 (UNKNOWN), not a pass.
- [if] semgrep scanned files but loaded 0 rules [then] exit 2.
- [if] an --expect-file path is absent from semgrep's paths.scanned [then] exit 2 and the
  message names it ("control files not scanned: <path>"), whether or not the file floor held.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

UNKNOWN_EXIT = 2
LOCKFILE_PATTERN = re.compile(
    r"(^|/)(uv\.lock|poetry\.lock|Pipfile\.lock|pylock(\.[^/]+)?\.toml|[^/]*requirements[^/]*\.txt"
    r"|pnpm-lock\.yaml|package-lock\.json|yarn\.lock|bun\.lockb?|Cargo\.lock|go\.sum|Gemfile\.lock)$"
)
CONTROL_PREFIX = "tests/fixtures/security/"


@dataclass(frozen=True)
class OsvFinding:
    source: str
    ecosystem: str
    package: str
    version: str
    vuln_id: str
    severity: str

    @property
    def identity(self) -> tuple[str, str, str]:
        """Identity for PR diffing: a vuln already on base is not new, even if the version moved."""
        return (self.ecosystem, self.package, self.vuln_id)

    def line(self) -> str:
        pkg = f"{self.ecosystem}/{self.package}@{self.version}"
        return f"{self.vuln_id} {pkg} ({self.source}) severity={self.severity or 'n/a'}"


# ----- helpers -----------------------------------------------------------------------------
def _unknown(message: str) -> int:
    print(f"UNKNOWN: {message}", file=sys.stderr)
    return UNKNOWN_EXIT


def _load_json(path: Path) -> object:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"{path} is empty")
    return json.loads(text)


def _emit(args: argparse.Namespace, title: str, lines: list[str], count: int) -> None:
    for line in lines:
        print(line)
    if args.count_file:
        Path(args.count_file).write_text(f"{count}\n", encoding="utf-8")
    if args.report_md:
        body = "\n".join(f"- `{line}`" for line in lines[:200]) or "- none"
        more = f"\n- ... {len(lines) - 200} more" if len(lines) > 200 else ""
        with Path(args.report_md).open("a", encoding="utf-8") as fh:
            fh.write(f"\n### {title} ({count})\n\n{body}{more}\n")


def _relative(path: str, root: str) -> str:
    return path[len(root) :].lstrip("/") if root and path.startswith(root) else path


def _scan_path_relative(path: str, root: Path) -> str:
    if path.startswith("./"):
        path = path[2:]
    root_text = str(root.resolve())
    if path.startswith(root_text):
        return path[len(root_text) :].lstrip("/")
    return path


def _osv_findings(doc: dict, root: str) -> tuple[list[OsvFinding], dict[str, int]]:
    findings: list[OsvFinding] = []
    parsed: dict[str, int] = {}
    for result in doc.get("results", []):
        source = _relative(result["source"]["path"], root)
        parsed[source] = parsed.get(source, 0) + len(result.get("packages", []))
        for pkg in result.get("packages", []):
            meta = pkg["package"]
            # One group = one advisory under all its aliases; report the first id.
            findings.extend(
                OsvFinding(
                    source=source,
                    ecosystem=meta["ecosystem"],
                    package=meta["name"],
                    version=meta.get("version", "?"),
                    vuln_id=sorted(group["ids"])[0],
                    severity=str(group.get("max_severity", "")),
                )
                for group in pkg.get("groups", [])
            )
    return findings, parsed


# ----- osv -------------------------------------------------------------------------------------
def cmd_osv_check(args: argparse.Namespace) -> int:
    try:
        doc = _load_json(Path(args.json))
    except (OSError, ValueError) as exc:
        return _unknown(f"osv-scanner JSON unreadable: {exc}")
    assert isinstance(doc, dict)
    findings, parsed = _osv_findings(doc, args.root)
    missing = [m for m in args.expect if parsed.get(m, 0) == 0]
    if missing:
        return _unknown(f"osv-scanner parsed no packages from: {', '.join(missing)}")
    print(f"parsed {len(parsed)} manifests, {sum(parsed.values())} packages")
    _emit(args, args.title, [f.line() for f in findings], len(findings))
    return 0


def cmd_osv_diff(args: argparse.Namespace) -> int:
    try:
        base_doc, head_doc = _load_json(Path(args.base)), _load_json(Path(args.head))
    except (OSError, ValueError) as exc:
        return _unknown(f"osv-scanner JSON unreadable: {exc}")
    assert isinstance(base_doc, dict) and isinstance(head_doc, dict)
    base, _ = _osv_findings(base_doc, args.base_root)
    head, parsed = _osv_findings(head_doc, args.root)
    if not parsed:
        return _unknown("osv-scanner head scan parsed nothing")
    known = {f.identity for f in base}
    new = [f for f in head if f.identity not in known]
    print(f"base findings {len(base)}, head findings {len(head)}, new {len(new)}")
    _emit(args, args.title, [f.line() for f in new], len(new))
    return 0


def cmd_osv_lint_config(args: argparse.Namespace) -> int:
    config = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    today = dt.datetime.now(dt.UTC).date()
    horizon = today + dt.timedelta(days=args.max_days)
    problems: list[str] = []
    entries = config.get("IgnoredVulns", [])
    for entry in entries:
        vid = entry.get("id", "<no id>")
        until = entry.get("ignoreUntil")
        if not str(entry.get("reason", "")).strip():
            problems.append(f"{vid}: missing reason")
        if until is None:
            problems.append(f"{vid}: missing ignoreUntil")
            continue
        until_date = until.date() if isinstance(until, dt.datetime) else until
        if until_date <= today:
            problems.append(f"{vid}: expired {until_date}; fix it or renew with a new reason")
        elif until_date > horizon:
            problems.append(
                f"{vid}: ignoreUntil {until_date} is more than {args.max_days} days out"
            )
    for problem in problems:
        print(f"[ERROR] osv-scanner.toml {problem}", file=sys.stderr)
    print(f"osv-scanner.toml: {len(entries)} IgnoredVulns entries, {len(problems)} problems")
    return UNKNOWN_EXIT if problems else 0


def cmd_inventory_check(args: argparse.Namespace) -> int:
    tracked = subprocess.run(
        ["git", "-C", args.root, "ls-files"], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    manifests = {
        p for p in tracked if LOCKFILE_PATTERN.search(p) and not p.startswith(CONTROL_PREFIX)
    }
    unlisted = sorted(manifests - set(args.expect))
    vanished = sorted(set(args.expect) - manifests)
    for path in unlisted:
        print(
            f"[ERROR] manifest not in the scan inventory: {path}"
            " (add it to MANIFESTS in scan_deps.sh)",
            file=sys.stderr,
        )
    for path in vanished:
        print(f"[ERROR] inventory lists a manifest git does not track: {path}", file=sys.stderr)
    print(f"inventory: {len(manifests)} tracked manifests, {len(args.expect)} listed")
    return UNKNOWN_EXIT if unlisted or vanished else 0


# ----- secrets ---------------------------------------------------------------------------------
def cmd_gitleaks_summary(args: argparse.Namespace) -> int:
    try:
        doc = _load_json(Path(args.json))
    except (OSError, ValueError) as exc:
        return _unknown(f"gitleaks JSON unreadable: {exc}")
    assert isinstance(doc, list)
    lines = [
        f"{f['RuleID']} {f['File']}:{f['StartLine']} commit={str(f.get('Commit', ''))[:9]}"
        for f in doc
    ]
    _emit(args, args.title, lines, len(doc))
    return 0


def cmd_trufflehog_summary(args: argparse.Namespace) -> int:
    path = Path(args.jsonl)
    if not path.exists():
        return _unknown(f"trufflehog output missing: {path}")
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip().startswith("{"):
            continue
        rec = json.loads(raw)
        if "DetectorName" not in rec:
            continue
        git = rec.get("SourceMetadata", {}).get("Data", {}).get("Git", {})
        state = "verified" if rec.get("Verified") else "unverified"
        where = f"{git.get('file', '?')}:{git.get('line', '?')}"
        commit = str(git.get("commit", ""))[:9]
        lines.append(f"{rec['DetectorName']} {state} {where} commit={commit}")
    _emit(args, args.title, lines, len(lines))
    return 0


# ----- sast / workflows ------------------------------------------------------------------------
def _path_excluded(path: str, prefixes: list[str]) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def _git_diff_names(root: Path, base: str, head: str) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", base, head],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _git_file_at_ref(root: Path, ref: str, rel_path: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"{ref}:{rel_path}"],
        capture_output=True,
    )
    return proc.returncode == 0


def _semgrep_scannable_count(
    *,
    root: Path,
    base: str,
    semgrep: str,
    configs: list[str],
    excludes: list[str],
    candidates: list[str],
) -> int:
    """Count the changed files the diff-aware scan will actually see.

    The real PR scan runs from the repo root with --baseline-commit and applies
    semgrep's default ignore list (tests/ among others). Naming changed files as
    explicit targets or --include patterns instead forces them past that list, so
    a tests/-only diff counted 1 here and scanned 0 there, and the mismatch
    read as UNKNOWN (job 104882711482 on PR #3362). Run the same root +
    --baseline-commit invocation and intersect paths.scanned with the git diff
    candidates.
    """
    cmd = [
        semgrep,
        "scan",
        *configs,
        "--json",
        "--time",
        "--metrics=off",
        "--disable-version-check",
        "--baseline-commit",
        base,
    ]
    for prefix in excludes:
        cmd.extend(["--exclude", prefix])
    cmd.append(".")
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
    if proc.returncode not in (0, 1):
        raise RuntimeError(
            f"semgrep exited {proc.returncode}: {proc.stderr.strip()[:200] or proc.stdout[:200]}"
        )
    if not proc.stdout.strip():
        raise RuntimeError("semgrep produced no JSON output")
    doc = json.loads(proc.stdout)
    if not isinstance(doc, dict):
        raise RuntimeError("semgrep JSON root is not an object")
    scanned = {_scan_path_relative(path, root) for path in doc.get("paths", {}).get("scanned", [])}
    return len(scanned & set(candidates))


def cmd_semgrep_diff_scope(args: argparse.Namespace) -> int:
    root = Path(args.root)
    candidates: list[str] = []
    for rel in _git_diff_names(root, args.base, args.head):
        if not _git_file_at_ref(root, args.head, rel):
            continue
        if _path_excluded(rel, args.exclude):
            continue
        candidates.append(rel)
    if not candidates:
        count = 0
        print(count)
        if args.count_file:
            Path(args.count_file).write_text(f"{count}\n", encoding="utf-8")
        return 0
    configs: list[str] = []
    for cfg in args.config:
        configs.extend(["--config", cfg])
    try:
        count = _semgrep_scannable_count(
            root=root,
            base=args.base,
            semgrep=args.semgrep,
            configs=configs,
            excludes=args.exclude,
            candidates=candidates,
        )
    except (OSError, RuntimeError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        return _unknown(f"semgrep diff scope unreadable: {exc}")
    print(count)
    if args.count_file:
        Path(args.count_file).write_text(f"{count}\n", encoding="utf-8")
    return 0


def _semgrep_rules_gate(
    args: argparse.Namespace,
    doc: dict,
    errors: list[dict],
    results: list[dict],
) -> int | None:
    rules = len(doc.get("time", {}).get("rules", [])) if "time" in doc else None
    if rules is None:
        # --expect-file is never skipped silently: no "time" block means it is unmeasured.
        no_time = 'semgrep JSON has no "time" block, so --expect-file could not be checked'
        return _unknown(no_time) if args.expect_file else None
    scanned_paths = doc.get("paths", {}).get("scanned", [])
    scanned = len(scanned_paths)
    # Computed before any success path, so no early exit 0 can bypass --expect-file.
    not_scanned = _expected_files_not_scanned(args.expect_file, scanned_paths)
    named = f"; control files not scanned: {', '.join(not_scanned)}" if not_scanned else ""
    print(f"semgrep loaded {rules} rules, scanned {scanned} files")
    expected = args.expected_scannable
    if expected is not None and expected > 0:
        if rules == 0:
            detail = (
                "and scanned 0: the changed paths may all be on semgrep's default "
                "ignore list; rerun with --verbose for paths.skipped"
                if scanned == 0
                else f"while scanning {scanned} file(s): the rule set failed to load"
            )
            return _unknown(
                f"semgrep expected {expected} scannable file(s) but loaded 0 rules {detail}"
            )
        if scanned == 0:
            if not errors and not results and not not_scanned:
                _emit(args, args.title, [], 0)
                return 0
            return _unknown(
                f"semgrep expected {expected} scannable file(s) but loaded {rules} rules "
                f"and scanned {scanned} files{named}"
            )
    if rules < args.min_rules:
        return _unknown(f"semgrep loaded {rules} rules, fewer than the {args.min_rules} floor")
    if scanned < args.min_files:
        return _unknown(
            f"semgrep scanned {scanned} files, fewer than the {args.min_files} floor{named}"
        )
    if not_scanned:
        return _unknown(f"semgrep scanned {scanned} files{named}")
    return None


def _expected_files_not_scanned(expected: list[str], scanned_paths: list[str]) -> list[str]:
    """Expected targets absent from paths.scanned, compared as the relative paths passed in.

    scan_sast.sh runs semgrep from the control root with root-relative targets, and semgrep
    echoes them back verbatim in paths.scanned (checked on 1.177.0), so no normalization.
    """
    scanned = set(scanned_paths)
    return [path for path in expected if path not in scanned]


def cmd_semgrep_summary(args: argparse.Namespace) -> int:
    try:
        doc = _load_json(Path(args.json))
    except (OSError, ValueError) as exc:
        return _unknown(f"semgrep JSON unreadable: {exc}")
    assert isinstance(doc, dict)
    errors = [e for e in doc.get("errors", []) if e.get("level") == "error"]
    for err in errors:
        print(
            f"[ERROR] semgrep: {err.get('type')} {str(err.get('message', ''))[:200]}",
            file=sys.stderr,
        )
    results = doc.get("results", [])
    gate = _semgrep_rules_gate(args, doc, errors, results)
    if gate is not None:
        return gate
    fired = Counter(r["check_id"].rsplit(".", 1)[-1] for r in results)
    silent = [rule for rule in args.require_rule if fired[rule] == 0]
    if silent:
        return _unknown(f"semgrep control did not fire for: {', '.join(silent)}")
    if errors and args.fail_on_error:
        return _unknown(f"semgrep reported {len(errors)} error(s)")
    lines = [f"{r['check_id']} {r['path']}:{r['start']['line']}" for r in results]
    _emit(args, args.title, lines, len(results))
    return 0


def cmd_zizmor_summary(args: argparse.Namespace) -> int:
    try:
        doc = _load_json(Path(args.json))
    except (OSError, ValueError) as exc:
        return _unknown(f"zizmor JSON unreadable: {exc}")
    assert isinstance(doc, list)
    lines = []
    for finding in doc:
        loc = finding["locations"][0]
        key = loc["symbolic"]["key"]
        path = (key.get("Local") or {}).get("verbatim_path") or json.dumps(key)
        row = loc["concrete"]["location"]["start_point"]["row"] + 1
        lines.append(f"{finding['determinations']['severity']} {finding['ident']} {path}:{row}")
    _emit(args, args.title, lines, len(lines))
    return 0


# ----- cli -------------------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name: str, func: object, **kwargs: object) -> argparse.ArgumentParser:
        p = sub.add_parser(name, **kwargs)  # type: ignore[arg-type]
        p.set_defaults(func=func)
        p.add_argument("--count-file")
        p.add_argument("--report-md")
        p.add_argument("--title", default=name)
        return p

    p = add("osv-check", cmd_osv_check)
    p.add_argument("json")
    p.add_argument("--root", required=True, help="absolute prefix to strip from source paths")
    p.add_argument("--expect", nargs="+", required=True)

    p = add("osv-diff", cmd_osv_diff)
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--base-root", required=True)

    p = add("osv-lint-config", cmd_osv_lint_config)
    p.add_argument("config")
    p.add_argument("--max-days", type=int, required=True)

    p = add("inventory-check", cmd_inventory_check)
    p.add_argument("--root", required=True)
    p.add_argument("--expect", nargs="+", required=True)

    p = add("gitleaks-summary", cmd_gitleaks_summary)
    p.add_argument("json")

    p = add("trufflehog-summary", cmd_trufflehog_summary)
    p.add_argument("jsonl")

    p = add("semgrep-summary", cmd_semgrep_summary)
    p.add_argument("json")
    p.add_argument("--require-rule", action="append", default=[])
    p.add_argument("--min-rules", type=int, default=1)
    p.add_argument("--min-files", type=int, default=0, help="fewer scanned files is UNKNOWN")
    p.add_argument(
        "--expect-file",
        action="append",
        default=[],
        help="repeatable; a path absent from paths.scanned is UNKNOWN and named",
    )
    p.add_argument("--fail-on-error", action="store_true")
    p.add_argument(
        "--expected-scannable",
        type=int,
        default=None,
        help="PR diff scope count; 0 rules or 0 scanned with N>0 is UNKNOWN",
    )

    p = add("semgrep-diff-scope", cmd_semgrep_diff_scope)
    p.add_argument("--root", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--head", default="HEAD")
    p.add_argument("--semgrep", required=True)
    p.add_argument("--config", action="append", required=True)
    p.add_argument("--exclude", action="append", default=[])

    p = add("zizmor-summary", cmd_zizmor_summary)
    p.add_argument("json")
    return parser


def main() -> int:
    args = _parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
