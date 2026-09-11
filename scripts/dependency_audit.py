"""Periodic dependency audit for row 8 of docs/ops/periodic-checks.md (#1507).

Runs pip-audit, cargo audit, and pnpm audit across the closures named in the
policy table. A tool that cannot run reports UNKNOWN and exits 3; a clean
closure exits 0; known CVE/GHSA/RUSTSEC findings exit 1 with advisory id and
import path on every line. Dev-only JS findings are labeled so triage is not
flattened.

    python -m scripts.dependency_audit
    python -m scripts.dependency_audit --json report.json

Exit codes: 0 clean, 1 findings, 3 UNKNOWN (never pass on empty output).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path

from scripts.dependency_audit_core import (
    _MADMOM_VCS,
    EXIT_FINDINGS,
    EXIT_OK,
    EXIT_UNKNOWN,
    ScanResult,
    parse_cargo_audit_json,
    parse_pip_audit_json,
    parse_pnpm_audit_json,
    summarize,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

PIP_AUDIT = [
    "uv",
    "run",
    "--no-project",
    "--isolated",
    "--with",
    "pip-audit",
    "pip-audit",
]


def _run(cmd: Sequence[str], *, cwd: Path | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        list(cmd),
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _unknown(name: str, detail: str) -> ScanResult:
    return ScanResult(name, "UNKNOWN", notes=(detail,))


def _export_pyproject_closure(path: Path) -> Path | None:
    code, stdout, stderr = _run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--extra",
            "all",
            "--no-emit-project",
            "--no-hashes",
            "--format",
            "requirements-txt",
            "-o",
            str(path),
        ]
    )
    if code != 0:
        return None
    if not path.is_file() or path.stat().st_size == 0:
        return None
    if stdout or stderr:
        pass
    return path


def scan_pip_requirements(name: str, req_file: Path) -> ScanResult:
    if not req_file.is_file():
        return _unknown(name, f"missing {req_file.relative_to(REPO_ROOT)}")
    code, stdout, stderr = _run([*PIP_AUDIT, "-r", str(req_file), "--format", "json"])
    if not stdout.strip():
        detail = stderr.strip() or f"pip-audit produced no output (exit {code})"
        return _unknown(name, detail)
    result = parse_pip_audit_json(stdout, ecosystem=name)
    if result.status == "OK" and code not in (0, 1):
        return _unknown(name, stderr.strip() or f"pip-audit exit {code}")
    return result


def scan_pip_pyproject_all() -> ScanResult:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix="-audit-reqs.txt", delete=False, dir=tempfile.gettempdir()
    ) as handle:
        export_path = Path(handle.name)
    try:
        if _export_pyproject_closure(export_path) is None:
            return _unknown("python-pyproject-all", "uv export --extra all failed")
        return scan_pip_requirements("python-pyproject-all", export_path)
    finally:
        export_path.unlink(missing_ok=True)


def scan_pip_requirements_txt() -> ScanResult:
    req = REPO_ROOT / "requirements.txt"
    result = scan_pip_requirements("python-requirements-txt", req)
    if result.status == "UNKNOWN":
        return result
    text = req.read_text(encoding="utf-8")
    notes = list(result.notes)
    if _MADMOM_VCS.search(text):
        notes.append(
            "madmom @ git+... is VCS-pinned; pip-audit cannot assess it "
            "(check CPJKU/madmom advisories by hand)"
        )
    if result.findings:
        return ScanResult(result.name, result.status, result.findings, tuple(notes))
    return ScanResult(result.name, result.status, (), tuple(notes))


def scan_cargo(lock_dir: Path, label: str) -> ScanResult:
    lock = lock_dir / "Cargo.lock"
    if not lock.is_file():
        return _unknown(label, f"missing {lock.relative_to(REPO_ROOT)}")
    code, stdout, stderr = _run(["cargo", "audit", "--json"], cwd=lock_dir)
    if not stdout.strip():
        detail = stderr.strip() or f"cargo audit produced no output (exit {code})"
        return _unknown(label, detail)
    result = parse_cargo_audit_json(stdout, ecosystem=label)
    if result.status == "OK" and code not in (0, 1):
        return _unknown(label, stderr.strip() or f"cargo audit exit {code}")
    return result


def scan_pnpm(project_dir: Path, label: str, *, audit_level: str | None) -> ScanResult:
    lock = project_dir / "pnpm-lock.yaml"
    if not lock.is_file():
        return _unknown(label, f"missing {lock.relative_to(REPO_ROOT)}")
    cmd = ["pnpm", "audit", "--json"]
    if audit_level:
        cmd.extend(["--audit-level", audit_level])
    code, stdout, stderr = _run(cmd, cwd=project_dir)
    if not stdout.strip():
        detail = stderr.strip() or f"pnpm audit produced no output (exit {code})"
        return _unknown(label, detail)
    result = parse_pnpm_audit_json(stdout, ecosystem=label)
    if result.status == "OK" and code not in (0, 1):
        return _unknown(label, stderr.strip() or f"pnpm audit exit {code}")
    return result


def run_all(
    *,
    runner: Callable[[], list[ScanResult]] | None = None,
) -> tuple[int, list[ScanResult], str]:
    if runner is not None:
        results = runner()
    else:
        results = [
            scan_pip_pyproject_all(),
            scan_pip_requirements_txt(),
            scan_cargo(REPO_ROOT / "apps/desktop/src-tauri", "rust-desktop-tauri"),
            scan_cargo(REPO_ROOT / "apps/webui/server/native/waveform", "rust-waveform-native"),
            scan_pnpm(REPO_ROOT / "apps/desktop", "js-desktop", audit_level=None),
            scan_pnpm(
                REPO_ROOT / "apps/webui/frontend",
                "js-webui-frontend",
                audit_level="high",
            ),
        ]
    exit_code, text = summarize(results)
    return exit_code, results, text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dependency_audit")
    parser.add_argument("--json", type=Path, default=None, help="write machine-readable report")
    args = parser.parse_args(argv)

    exit_code, results, text = run_all()
    print(text)
    if args.json:
        payload = {
            "exit_code": exit_code,
            "results": [
                {
                    **asdict(r),
                    "findings": [asdict(f) for f in r.findings],
                }
                for r in results
            ],
        }
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if exit_code == EXIT_UNKNOWN:
        return EXIT_UNKNOWN
    if exit_code == EXIT_FINDINGS:
        return EXIT_FINDINGS
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
