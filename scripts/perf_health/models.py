"""Result types shared by every perf-health check."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import SEVERITY_ERROR


class PreconditionError(RuntimeError):
    """Raised when a path exists but is not the kind of thing it must be. Never swallowed."""


@dataclass
class Finding:
    """One evidence row plus the pointer saying which other signal to correlate it with."""

    when: str
    signature: str
    message: str
    context: str
    pointer: str

    def evidence_lines(self) -> list[str]:
        head = f"  {self.when}  {self.signature}: {self.message}"
        lines = [head]
        if self.context:
            lines.append(f"      context: {self.context}")
        lines.append(f"      -> {self.pointer}")
        return lines


@dataclass
class CheckResult:
    """Verdict for one named sink, carrying its own window and denominator."""

    check: str
    severity: str
    classification: str
    detail: str
    window: str
    sources: list[str] = field(default_factory=list)
    breakdown: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    remediation: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity == SEVERITY_ERROR

    def verdict_line(self) -> str:
        return f"[{self.severity}] {self.check}: {self.detail}"
