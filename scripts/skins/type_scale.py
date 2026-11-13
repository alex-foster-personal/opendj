"""Type-scale ratio error between two skins (skin likeness signal, PLAN.md section 6).

Each side is a ``{role: px}`` map of font sizes. Every role is divided by the base role,
so a skin scaled by a constant has the same ratios. The error for a role is the absolute
log of the candidate ratio over the reference ratio; the mean is taken over the roles
present on both sides (the base role is excluded).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_BASE = "body"


@dataclass(frozen=True)
class TypeScaleError:
    per_role: dict[str, float]
    mean: float


def _check(sizes: Mapping[str, float], side: str) -> None:
    for role, px in sizes.items():
        if isinstance(px, bool) or not isinstance(px, int | float) or not math.isfinite(px) or px <= 0:
            raise ValueError(f"{side} font size for role {role!r} must be a positive number, got {px!r}")


def type_scale_error(
    reference: Mapping[str, float],
    candidate: Mapping[str, float],
    base: str = DEFAULT_BASE,
) -> TypeScaleError:
    for side, sizes in (("reference", reference), ("candidate", candidate)):
        if base not in sizes:
            raise ValueError(f"{side} is missing the base role {base!r}")
        _check(sizes, side)
    ref_base, cand_base = reference[base], candidate[base]
    per_role = {
        role: abs(math.log((candidate[role] / cand_base) / (reference[role] / ref_base)))
        for role in sorted(set(reference) & set(candidate))
        if role != base
    }
    mean = sum(per_role.values()) / len(per_role) if per_role else 0.0
    return TypeScaleError(per_role=per_role, mean=mean)
