"""Core types and parsers for :mod:`scripts.dependency_audit` (row 8, #1507)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_UNKNOWN = 3

# Path segments that indicate a pnpm advisory is dev-only (Apr 2026 pass: 9/14).
_DEV_PATH_MARKERS = (
    "@storybook/",
    "storybook>",
    ">vitest",
    ">eslint",
    "@playwright/",
    ">playwright",
    ">prettier",
    ">typescript-eslint",
    ">@types/",
)


@dataclass(frozen=True)
class Finding:
    ecosystem: str
    advisory_id: str
    package: str
    path: str
    dev_only: bool
    note: str = ""

    def render(self) -> str:
        scope = "dev-only" if self.dev_only else "runtime"
        line = f"  [{scope}] {self.advisory_id} {self.package}: {self.path}"
        if self.note:
            line += f" ({self.note})"
        return line


@dataclass(frozen=True)
class ScanResult:
    name: str
    status: str  # OK | FINDINGS | UNKNOWN
    findings: tuple[Finding, ...] = ()
    notes: tuple[str, ...] = ()

    def render(self) -> str:
        lines = [f"[{self.name}] {self.status}"]
        lines.extend(f"  note: {note}" for note in self.notes)
        lines.extend(finding.render() for finding in self.findings)
        return "\n".join(lines)


def _pnpm_path_dev_only(path: str) -> bool:
    lowered = path.lower()
    return any(marker in lowered for marker in _DEV_PATH_MARKERS)


def parse_pip_audit_json(payload: str, *, ecosystem: str) -> ScanResult:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        return ScanResult(ecosystem, "UNKNOWN", notes=(f"invalid JSON: {exc}",))

    if not isinstance(data, dict):
        return ScanResult(ecosystem, "UNKNOWN", notes=("expected JSON object",))

    deps = data.get("dependencies")
    if deps is None:
        return ScanResult(ecosystem, "UNKNOWN", notes=("missing dependencies key",))
    if not isinstance(deps, list):
        return ScanResult(ecosystem, "UNKNOWN", notes=("dependencies is not a list",))

    findings: list[Finding] = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("name") or "unknown")
        version = str(dep.get("version") or "")
        vulns = dep.get("vulns") or []
        if not vulns:
            continue
        for vuln in vulns:
            if not isinstance(vuln, dict):
                continue
            adv_id = str(vuln.get("id") or vuln.get("aliases", ["UNKNOWN"])[0])
            path = f"{name}=={version}" if version else name
            findings.append(
                Finding(
                    ecosystem=ecosystem,
                    advisory_id=adv_id,
                    package=name,
                    path=path,
                    dev_only=False,
                )
            )

    if findings:
        return ScanResult(ecosystem, "FINDINGS", tuple(findings))
    return ScanResult(ecosystem, "OK")


def parse_cargo_audit_json(payload: str, *, ecosystem: str) -> ScanResult:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        return ScanResult(ecosystem, "UNKNOWN", notes=(f"invalid JSON: {exc}",))

    vulnerabilities = data.get("vulnerabilities")
    if vulnerabilities is None:
        return ScanResult(ecosystem, "UNKNOWN", notes=("missing vulnerabilities key",))

    listed = vulnerabilities.get("list") or vulnerabilities.get("listed") or []
    if not isinstance(listed, list):
        return ScanResult(ecosystem, "UNKNOWN", notes=("vulnerabilities.list is not a list",))

    findings: list[Finding] = []
    for item in listed:
        if not isinstance(item, dict):
            continue
        advisory = item.get("advisory") or {}
        adv_id = str(advisory.get("id") or "RUSTSEC-UNKNOWN")
        package = item.get("package") or {}
        name = str(package.get("name") or "unknown")
        version = str(package.get("version") or "")
        dep_path = " > ".join(str(p) for p in (item.get("dependencies") or []) if p)
        if not dep_path:
            dep_path = f"{name} {version}".strip()
        findings.append(
            Finding(
                ecosystem=ecosystem,
                advisory_id=adv_id,
                package=name,
                path=dep_path,
                dev_only=False,
            )
        )

    if findings:
        return ScanResult(ecosystem, "FINDINGS", tuple(findings))
    return ScanResult(ecosystem, "OK")


def parse_pnpm_audit_json(payload: str, *, ecosystem: str) -> ScanResult:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        return ScanResult(ecosystem, "UNKNOWN", notes=(f"invalid JSON: {exc}",))

    advisories = data.get("advisories")
    if advisories is None:
        return ScanResult(ecosystem, "UNKNOWN", notes=("missing advisories key",))
    if not isinstance(advisories, dict):
        return ScanResult(ecosystem, "UNKNOWN", notes=("advisories is not an object",))

    findings: list[Finding] = []
    for key, advisory in advisories.items():
        if not isinstance(advisory, dict):
            continue
        adv_id = str(advisory.get("cves", [key])[0] if advisory.get("cves") else key)
        if advisory.get("github_advisory_id"):
            adv_id = str(advisory["github_advisory_id"])
        module_name = str(advisory.get("module_name") or advisory.get("title") or key)
        for item in advisory.get("findings") or []:
            if not isinstance(item, dict):
                continue
            version = str(item.get("version") or "")
            for path in item.get("paths") or ["."]:
                path_str = str(path)
                findings.append(
                    Finding(
                        ecosystem=ecosystem,
                        advisory_id=adv_id,
                        package=f"{module_name}@{version}" if version else module_name,
                        path=path_str,
                        dev_only=_pnpm_path_dev_only(path_str),
                    )
                )

    if findings:
        return ScanResult(ecosystem, "FINDINGS", tuple(findings))
    return ScanResult(ecosystem, "OK")


def summarize(results: list[ScanResult]) -> tuple[int, str]:
    lines: list[str] = []
    exit_code = EXIT_OK
    for result in results:
        lines.append(result.render())
        if result.status == "UNKNOWN":
            exit_code = EXIT_UNKNOWN
        elif result.status == "FINDINGS" and exit_code != EXIT_UNKNOWN:
            exit_code = EXIT_FINDINGS

    dev_count = sum(1 for r in results for f in r.findings if f.dev_only)
    runtime_count = sum(1 for r in results for f in r.findings if not f.dev_only)
    lines.append(
        f"[dependency-audit] findings={runtime_count + dev_count} "
        f"(runtime={runtime_count} dev-only={dev_count})"
    )
    unknown = [r.name for r in results if r.status == "UNKNOWN"]
    if unknown:
        lines.append(f"[dependency-audit] UNKNOWN scans: {', '.join(unknown)}")
    return exit_code, "\n".join(lines)


_MADMOM_VCS = re.compile(r"madmom\s*@\s*git\+", re.I)
