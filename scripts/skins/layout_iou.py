"""Region bounding-box IoU score for skin likeness.

Boxes are ``[x, y, w, h]`` in a shared coordinate space.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

Box = Sequence[float]


@dataclass(frozen=True)
class LayoutIoU:
    per_region: dict[str, float]
    mean: float
    missing: list[str] = field(default_factory=list)


def _check(region: str, box: Box) -> None:
    if len(box) != 4:
        raise ValueError(f"region {region!r}: box must be [x, y, w, h]")
    if box[2] <= 0 or box[3] <= 0:
        raise ValueError(f"region {region!r}: width and height must be positive")


def box_iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union


def layout_iou(reference: Mapping[str, Box], candidate: Mapping[str, Box]) -> LayoutIoU:
    for region, box in (*reference.items(), *candidate.items()):
        _check(region, box)
    per_region: dict[str, float] = {}
    missing: list[str] = []
    for region, ref_box in reference.items():
        cand_box = candidate.get(region)
        if cand_box is None:
            missing.append(region)
            per_region[region] = 0.0
        else:
            per_region[region] = box_iou(ref_box, cand_box)
    mean = sum(per_region.values()) / len(per_region) if per_region else 0.0
    return LayoutIoU(per_region=per_region, mean=mean, missing=missing)
