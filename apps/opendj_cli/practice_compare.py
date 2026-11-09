"""Python twin of ``practice-compare.ts`` (issue #9406).

Vectors are pinned in ``tests/skins/fixtures/practice-compare-vectors.json``;
keep this module aligned with
``apps/webui/frontend/src/lib/components/rb/practice/practice-compare.ts``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableSequence, Sequence
from typing import Any, Literal, cast

CompareSource = Literal["replica", "reference"]
_M = 0xFFFFFFFF


def _imul(a: int, b: int) -> int:
    return (a * b) & _M


def mulberry32(seed: int) -> Callable[[], float]:
    """Same stream as the TypeScript ``mulberry32`` (32-bit ops, divide by 2**32)."""

    t = seed & _M

    def step() -> float:
        nonlocal t
        t = (t + 0x6D2B79F5) & _M
        x = t
        x = _imul(x ^ (x >> 15), x | 1)
        x = (x ^ (x + _imul(x ^ (x >> 7), x | 61))) & _M
        return ((x ^ (x >> 14)) & _M) / 4294967296.0

    return step


def shuffled_order(items: MutableSequence[Any], rand: Callable[[], float]) -> list[Any]:
    out = list(items)
    for i in range(len(out) - 1, 0, -1):
        j = int(rand() * (i + 1))
        out[i], out[j] = out[j], out[i]
    return out


def blind_trials(panel: Mapping[str, float], seed: int, cols: int = 3, rows: int = 2) -> list[dict[str, Any]]:
    cell_w = panel["w"] / cols
    cell_h = panel["d"] / rows
    rand = mulberry32(seed)
    trials: list[dict[str, Any]] = []
    n = 0
    for row in range(rows):
        for col in range(cols):
            n += 1
            x = col * cell_w
            y = row * cell_h
            order_list = shuffled_order(["replica", "reference"], rand)
            order = cast(
                tuple[CompareSource, CompareSource],
                (order_list[0], order_list[1]),
            )
            trials.append(
                {
                    "trial_id": f"t{n}",
                    "region": {"x": x, "y": y, "w": cell_w, "h": cell_h},
                    "order": list(order),
                    "rating": None,
                    "choice": None,
                }
            )
    return trials


def record_answer(trials: Sequence[Mapping[str, Any]], trial_id: str, letter: str, rating: int) -> list[dict[str, Any]]:
    if not isinstance(rating, int) or rating < 1 or rating > 5:
        raise ValueError("rating must be an integer from 1 to 5")
    idx = next((i for i, t in enumerate(trials) if t["trial_id"] == trial_id), -1)
    if idx < 0:
        raise ValueError(f"unknown trial id {trial_id}")
    t = trials[idx]
    order = t["order"]
    if letter == "A":
        choice = cast(CompareSource, order[0])
    elif letter == "B":
        choice = cast(CompareSource, order[1])
    else:
        raise ValueError("letter must be A or B")
    out: list[dict[str, Any]] = []
    for i, row in enumerate(trials):
        d = dict(row)
        if i == idx:
            d["choice"] = choice
            d["rating"] = rating
        out.append(d)
    return out


def compare_scale_px_per_mm(override: float | None) -> float:
    """Match ``compareScale`` when no image size is available (CLI has no decoded image)."""

    if override is not None and override > 0:
        return round(override * 100) / 100
    return 2.0


def build_compare_report(
    *,
    device: str,
    ref: str,
    seed: int,
    px_per_mm: float,
    trials: Sequence[Mapping[str, Any]],
    recorded_at: str,
) -> dict[str, Any]:
    answered = [t for t in trials if t.get("rating") is not None and t.get("choice") is not None]
    return {
        "format": "opendj-practice-compare/1",
        "device": device,
        "ref": ref,
        "seed": seed,
        "px_per_mm": px_per_mm,
        "recorded_at": recorded_at,
        "trials": [
            {
                "trial_id": t["trial_id"],
                "region": dict(t["region"]),
                "order": list(t["order"]),
                "rating": t["rating"],
                "choice": t["choice"],
            }
            for t in answered
        ],
    }
