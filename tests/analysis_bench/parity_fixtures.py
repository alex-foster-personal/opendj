"""Hermetic three-host round-table fixtures (tests only; issue #12159)."""

from __future__ import annotations

from typing import Any

from apps.analysis_bench import parity


def synthetic_host_reports(base_report: dict[str, Any]) -> dict[str, Any]:
    """Three-host fixture bundle for tests without live CI on every target."""
    arms = base_report.get("arms") or {}
    scorer_version = base_report.get("scorer_version", "?")
    bundle = base_report.get("bundle")
    shared: dict[str, Any] = {
        "scorer_version": scorer_version,
        "bundle": bundle,
        "arms": arms,
    }
    return {
        parity.HOST_MACOS_ARM64: {
            "identity": {
                "system": "darwin",
                "machine": "arm64",
                "wsl": False,
                "node": "fixture-macos",
            },
            **shared,
        },
        parity.HOST_LINUX_X86_64: {
            "identity": {
                "system": "linux",
                "machine": "x86_64",
                "wsl": False,
                "node": "fixture-linux",
            },
            **shared,
        },
        parity.HOST_WINDOWS_X86_64: {
            "identity": {
                "system": "windows",
                "machine": "x86_64",
                "wsl": False,
                "node": "fixture-windows",
            },
            **shared,
        },
    }


def attach_synthetic_hosts(report: dict[str, Any]) -> dict[str, Any]:
    """Fill every required column for hermetic round-table tests."""
    report = dict(report)
    report.setdefault("lane_table", report.get("table", ""))
    report["host_reports"] = synthetic_host_reports(report)
    report["table"] = parity.render_host_matrix_table(report)
    return report
