"""[if] type_scale_error misreports the type-scale ratio error [then] broken, [else stop]."""

from __future__ import annotations

import math

import pytest

from scripts.skins.type_scale import type_scale_error

pytestmark = pytest.mark.requirement("SKIN-14573")

REF = {"body": 14.0, "h1": 28.0, "label": 11.0}


def test_constant_scale_is_zero_error() -> None:
    """[if] same sizes scaled by a constant do not give 0.0 error [then] broken, [else stop]."""
    cand = {role: px * 1.5 for role, px in REF.items()}
    result = type_scale_error(REF, cand)
    assert result.mean == pytest.approx(0.0, abs=1e-12)
    assert all(v == pytest.approx(0.0, abs=1e-12) for v in result.per_role.values())


def test_doubled_role_error_is_ln2() -> None:
    """[if] a role doubled relative to base does not give ln 2 [then] broken, [else stop]."""
    cand = {"body": 14.0, "h1": 56.0, "label": 11.0}
    result = type_scale_error(REF, cand)
    assert abs(result.per_role["h1"] - math.log(2)) < 1e-9
    assert result.per_role["label"] == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize("side", ["reference", "candidate"])
def test_missing_base_raises_naming_base(side: str) -> None:
    """[if] a missing base role does not raise naming it [then] broken, [else stop]."""
    no_base = {"h1": 28.0}
    args = (no_base, REF) if side == "reference" else (REF, no_base)
    with pytest.raises(ValueError, match="'body'"):
        type_scale_error(*args)


@pytest.mark.parametrize("bad", [0, -3.0, "12px", float("nan")])
def test_bad_size_raises_naming_role(bad: object) -> None:
    """[if] a bad size does not raise naming the role [then] broken, [else stop]."""
    cand = {"body": 14.0, "h1": bad}
    with pytest.raises(ValueError, match="'h1'"):
        type_scale_error(REF, cand)  # type: ignore[arg-type]
