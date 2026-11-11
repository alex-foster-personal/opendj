"""Three-host round tables and promotion gates (NATIVE-13 slice, issue #11227).

Every posted round carries one column per required host plus a parity row naming
the section 13 tolerances. Promotion refuses until the latest round's table
includes that parity row and all three host columns.
"""

from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.analysis_bench.rounds import RoundError, _HEADING_RE

HOST_MACOS_ARM64 = "macos_arm64"
HOST_LINUX_X86_64 = "linux_x86_64"
HOST_WINDOWS_X86_64 = "windows_x86_64"

REQUIRED_HOST_COLUMNS: tuple[str, ...] = (
    HOST_MACOS_ARM64,
    HOST_LINUX_X86_64,
    HOST_WINDOWS_X86_64,
)

COLUMN_TITLES: dict[str, str] = {
    HOST_MACOS_ARM64: "macOS arm64",
    HOST_LINUX_X86_64: "Linux x86_64",
    HOST_WINDOWS_X86_64: "Windows x86_64 native",
}

PARITY_ROW_LABEL = "parity"
PARITY_TOLERANCE_NOTE = "beats 5 ms, BPM 0.01, key equal, LUFS 0.05 LU, dBTP 0.05 dB"


@dataclass(frozen=True)
class HostIdentity:
    """One machine posting into the round-table matrix."""

    system: str
    machine: str
    wsl: bool = False
    node: str = ""

    def column_key(self) -> str:
        system = self.system.lower()
        machine = self.machine.lower()
        if machine == "amd64":
            machine = "x86_64"
        if system == "darwin":
            return HOST_MACOS_ARM64
        if system == "linux":
            return HOST_LINUX_X86_64
        if system == "windows":
            return HOST_WINDOWS_X86_64
        raise RoundError(
            f"unsupported host identity {self.node} ({system} {machine}); "
            "round tables accept only macOS arm64, Linux x86_64 and Windows x86_64 native"
        )


def detect_wsl() -> bool:
    """True when this Linux process is WSL rather than a native x86_64 host."""
    if platform.system().lower() != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def current_host_identity() -> HostIdentity:
    """The column key material for the machine running this command."""
    return HostIdentity(
        system=platform.system().lower(),
        machine=platform.machine().lower(),
        wsl=detect_wsl(),
        node=platform.node(),
    )


def identity_from_mapping(payload: dict[str, Any]) -> HostIdentity:
    """Rehydrate a host identity stored in a report JSON artifact."""
    return HostIdentity(
        system=str(payload.get("system", "")).lower(),
        machine=str(payload.get("machine", "")).lower(),
        wsl=bool(payload.get("wsl", False)),
        node=str(payload.get("node", "")),
    )


def validate_column_identity(column: str, identity: HostIdentity) -> None:
    """Refuse host columns whose recorded identity does not match the matrix."""
    machine = identity.machine.lower()
    if machine == "amd64":
        machine = "x86_64"
    system = identity.system.lower()

    if column == HOST_MACOS_ARM64:
        if system != "darwin" or machine != "arm64":
            raise RoundError(f"macOS column requires uname machine arm64, got {system} {machine}")
        return
    if column == HOST_LINUX_X86_64:
        if system != "linux" or machine != "x86_64":
            raise RoundError(f"Linux column requires linux x86_64, got {system} {machine}")
        return
    if column == HOST_WINDOWS_X86_64:
        if system != "windows" or machine != "x86_64":
            raise RoundError(f"Windows column requires native x86_64, got {system} {machine}")
        if identity.wsl:
            raise RoundError(
                "Windows column must be native x86_64, not WSL; "
                "bench runs on WSL read bundles from the Linux filesystem"
            )


def _candidate_fixed_f(arms: dict[str, Any]) -> str:
    for arm in arms.values():
        if arm.get("role") != "candidate":
            continue
        fixed = arm.get("fixed") or {}
        value = fixed.get("f_measure_mean")
        if value is None:
            payload = arm.get("payload") or {}
            fixed = payload.get("fixed") or {}
            value = fixed.get("f_measure_mean")
        if value is not None:
            return f"{float(value):.3f}"
    return "-"


def _arm_role_map(arms: dict[str, Any]) -> dict[str, str | None]:
    """Arm names and roles only; used to compare host columns to the posting report."""
    return {name: (arm.get("role") if isinstance(arm, dict) else None) for name, arm in sorted(arms.items())}


def validate_host_reports(host_reports: dict[str, Any], *, report: dict[str, Any]) -> None:
    """Every required column is present, identities match, and provenance agrees with `report`."""
    missing = [key for key in REQUIRED_HOST_COLUMNS if key not in host_reports]
    if missing:
        names = ", ".join(COLUMN_TITLES[key] for key in missing)
        raise RoundError(
            f"round table is missing host column(s) {names}; all three hosts are required before a round can post"
        )

    report_scorer = report.get("scorer_version")
    if not report_scorer:
        raise RoundError("report has no scorer_version; host columns cannot be validated")
    report_bundle = report.get("bundle") or {}
    report_arm_map = _arm_role_map(report.get("arms") or {})

    for column in REQUIRED_HOST_COLUMNS:
        title = COLUMN_TITLES[column]
        entry = host_reports[column]
        if not isinstance(entry, dict):
            raise RoundError(f"host report for {title} is not an object")
        identity = identity_from_mapping(entry.get("identity") or entry.get("host") or {})
        validate_column_identity(column, identity)

        arms = entry.get("arms")
        if not isinstance(arms, dict) or not arms:
            raise RoundError(
                f"host report for {title} has no measured arms; each host column must carry that host's own scores"
            )

        entry_scorer = entry.get("scorer_version")
        if entry_scorer != report_scorer:
            raise RoundError(
                f"host report for {title} has scorer {entry_scorer!r}, but this round uses scorer {report_scorer!r}"
            )

        entry_bundle = entry.get("bundle") or {}
        for key in ("bundle_id", "lane"):
            expected = report_bundle.get(key)
            actual = entry_bundle.get(key)
            if actual != expected:
                raise RoundError(
                    f"host report for {title} has bundle {key} {actual!r}, but this round uses {expected!r}"
                )
        if "version" in report_bundle or "version" in entry_bundle:
            expected = report_bundle.get("version")
            actual = entry_bundle.get("version")
            if actual != expected:
                raise RoundError(
                    f"host report for {title} has bundle version {actual!r}, but this round uses {expected!r}"
                )

        entry_arm_map = _arm_role_map(arms)
        if entry_arm_map != report_arm_map:
            raise RoundError(
                f"host report for {title} arm set {entry_arm_map!r} does not match this round's arms {report_arm_map!r}"
            )


def render_host_matrix_table(report: dict[str, Any]) -> str:
    """Markdown matrix: scorer rows per host plus the parity row."""
    host_reports = report.get("host_reports") or {}
    validate_host_reports(host_reports, report=report)

    header = "| metric | " + " | ".join(COLUMN_TITLES[key] for key in REQUIRED_HOST_COLUMNS) + " |"
    delimiter = "|---|" + "|".join("---" for _ in REQUIRED_HOST_COLUMNS) + "|"

    hosts_row = ["host"]
    scorer_row = ["scorer"]
    candidate_row = ["candidate fixed F"]
    for column in REQUIRED_HOST_COLUMNS:
        entry = host_reports[column]
        identity = identity_from_mapping(entry.get("identity") or entry.get("host") or {})
        label = identity.node or COLUMN_TITLES[column]
        hosts_row.append(f"`{label}` ({identity.machine})")
        scorer_row.append(str(entry["scorer_version"]))
        candidate_row.append(_candidate_fixed_f(entry["arms"]))

    parity_cells = ["**parity**", "pending", "pending", "pending"]
    lines = [
        header,
        delimiter,
        "| " + " | ".join(hosts_row) + " |",
        "| " + " | ".join(scorer_row) + " |",
        "| " + " | ".join(candidate_row) + " |",
        "| " + " | ".join(parity_cells) + " |",
        "",
        f"Parity tolerances: {PARITY_TOLERANCE_NOTE}.",
    ]
    lane_table = (report.get("lane_table") or "").strip()
    if lane_table:
        lines.extend(["", lane_table])
    return "\n".join(line for line in lines if line is not None).rstrip()


def table_has_three_host_columns(table: str) -> bool:
    """True when a markdown row names all three host columns."""
    for line in table.splitlines():
        lower = line.lower()
        if all(COLUMN_TITLES[key].lower() in lower for key in REQUIRED_HOST_COLUMNS):
            return True
    return False


def table_has_parity_row(table: str) -> bool:
    """True when a table row begins with the parity label."""
    for line in table.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip().strip("*").lower() for cell in line.split("|")[1:-1]]
        if cells and cells[0] == PARITY_ROW_LABEL:
            return True
    return False


def validate_posted_table(table: str) -> None:
    """Refuse tables that omit a required host column or the parity row."""
    if not table_has_three_host_columns(table):
        raise RoundError(
            "round table has fewer than three host columns; "
            "macOS arm64, Linux x86_64 and Windows x86_64 native are all required"
        )
    if not table_has_parity_row(table):
        raise RoundError("round table has no parity row; promotion is refused until tolerances are recorded")


def stamp_current_host_report(
    report: dict[str, Any],
    *,
    identity: HostIdentity | None = None,
) -> dict[str, Any]:
    """Record this machine's column slice before posting a round."""
    identity = identity or current_host_identity()
    column = identity.column_key()
    host_reports = dict(report.get("host_reports") or {})
    host_reports[column] = {
        "identity": {
            "system": identity.system,
            "machine": identity.machine,
            "wsl": identity.wsl,
            "node": identity.node,
        },
        "scorer_version": report.get("scorer_version"),
        "bundle": report.get("bundle"),
        "arms": report.get("arms"),
    }
    report["host_reports"] = host_reports
    report["table"] = render_host_matrix_table(report)
    return report


def latest_round_block(log_text: str, lane: str) -> str | None:
    """Body of the newest round heading for `lane`, including every table in it."""
    matches = [match for match in _HEADING_RE.finditer(log_text) if match.group("lane") == lane]
    if not matches:
        return None
    start = matches[-1].end()
    tail = log_text[start:]
    next_heading = re.search(r"^###\s+", tail, re.M)
    block = tail[: next_heading.start()] if next_heading else tail
    stripped = block.strip()
    return stripped or None


def assert_promotion_allowed(log_path: Path, lane: str) -> None:
    """Refuse promotion when the latest round lacks a parity row or host columns."""
    path = Path(log_path)
    if not path.exists():
        raise RoundError(f"{path} does not exist; cannot promote a lane with no experiment log")
    text = path.read_text(encoding="utf-8")
    block = latest_round_block(text, lane)
    if block is None:
        raise RoundError(f"{lane} has no posted round in {path}; promotion needs a round table to read")
    validate_posted_table(block)
