"""Parse scanner JSON for scripts/security/*.sh. Stdlib only, Python 3.11+.

Every subcommand prints human-readable detail lines to stdout, appends a markdown
section to --report-md, and writes the in-scope finding count to --count-file.
Secret values are never read into output: gitleaks/trufflehog summaries print rule
or detector ids, file paths, lines and short commit ids only.

Exit codes: 0 = parsed (findings may be > 0), 2 = UNKNOWN (input could not be
measured: missing file, malformed JSON, expected subject absent, control silent),
3 = SKIP (semgrep-summary --skip-if-nothing-scanned only: the scope held no file
semgrep scans, so no rules loaded and nothing was measured).

Acceptance (one assertion each, exercised by `just security-scan`):
- [if] osv JSON lacks one of the expected manifests [then] exit 2 (UNKNOWN).
- [if] an IgnoredVulns entry lacks reason/ignoreUntil or expires > max days out [then] exit 2.
- [if] a head finding shares (ecosystem, package, id) with base [then] it is not NEW.
- [if] a semgrep control run lacks a required rule id [then] exit 2.
- [if] a diff-aware semgrep run scanned 0 files and loaded 0 rules under
  --skip-if-nothing-scanned [then] exit 3 (SKIP), not 2.
- [if] semgrep scanned files but loaded 0 rules [then] exit 2, with or without that flag.
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
SKIP_EXIT = 3
LOCKFILE_PATTERN = re.compile(
    r"(^|/)(uv\.lock|poetry\.lock|Pipfile\.lock|pylock\.toml|[^/]*requirements[^/]*\.txt"
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
    rules = len(doc.get("time", {}).get("rules", [])) if "time" in doc else None
    if rules is not None:
        scanned = len(doc.get("paths", {}).get("scanned", []))
        print(f"semgrep loaded {rules} rules, scanned {scanned} files")
        # semgrep loads rules per language present, so an empty scope loads none.
        # Only then is a zero rule count an answer rather than a failed download.
        if args.skip_if_nothing_scanned and scanned == 0 and rules == 0:
            _emit(args, args.title, ["no file in scope that semgrep scans"], 0)
            return SKIP_EXIT
        if rules < args.min_rules:
            return _unknown(f"semgrep loaded {rules} rules, fewer than the {args.min_rules} floor")
        if scanned < args.min_files:
            return _unknown(
                f"semgrep scanned {scanned} files, fewer than the {args.min_files} floor"
            )
    results = doc.get("results", [])
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
    p.add_argument("--fail-on-error", action="store_true")
    p.add_argument(
        "--skip-if-nothing-scanned",
        action="store_true",
        help="exit 3 (SKIP) when 0 files were scanned and 0 rules loaded",
    )

    p = add("zizmor-summary", cmd_zizmor_summary)
    p.add_argument("json")
    return parser


def main() -> int:
    args = _parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
