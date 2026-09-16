"""PERFMODE-03 Python/TS app-posture scaler parity."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apps.shared import app_posture

REPO_ROOT = Path(__file__).resolve().parents[2]
TS_PATH = REPO_ROOT / "apps/webui/frontend/src/lib/rb/app-posture.ts"
_TS_NUMERIC_CONST = re.compile(
    r"^export const ([A-Z][A-Z0-9_]*) = ([0-9_]+(?: \* [0-9_]+)*);$", re.M
)


def _ts_numeric_constants(text: str) -> dict[str, int]:
    """Every exported numeric constant, evaluating products such as ``24 * 1024 * 1024``."""
    constants: dict[str, int] = {}
    for name, expr in _TS_NUMERIC_CONST.findall(text):
        value = 1
        for factor in expr.split("*"):
            value *= int(factor.strip().replace("_", ""))
        constants[name] = value
    return constants


@pytest.mark.requirement("PERFMODE-03")
def test_ts_posture_constants_match_python() -> None:
    """[if] TS and Python posture constants differ, or exist on one side only
    [then] parity test fails, [else stop].

    Every exported TS constant is compared rather than a hardcoded list: the
    earlier four-name version would have let a new Gig cap exist on one side only.
    """
    ts = _ts_numeric_constants(TS_PATH.read_text(encoding="utf-8"))
    py = {
        name: value
        for name, value in vars(app_posture).items()
        if name.isupper() and isinstance(value, int)
    }
    assert len(ts) >= 5, f"if the TS parser finds almost nothing then it is vacuous - broken: {ts}"
    assert ts.keys() == py.keys(), (
        "if a posture constant exists in one language only then Gig caps differ - broken: "
        f"TS only {sorted(ts.keys() - py.keys())}, python only {sorted(py.keys() - ts.keys())}"
    )
    for name, value in py.items():
        assert ts[name] == value, (
            f"if {name} differs (TS {ts[name]} vs python {value}) then posture caps drift - broken"
        )
