"""Shared sort keys for plate fig ids across deck-diagram scripts."""
from __future__ import annotations


def fig_sort_key(fig: str) -> tuple[int | str, ...]:
    """Order section ids (1-1), letter ids (D1), then everything else."""
    if "-" in fig and fig[0].isdigit():
        section, index = fig.split("-", 1)
        return (0, int(section), int(index))
    if len(fig) >= 2 and fig[0].isalpha() and fig[1:].isdigit():
        return (1, fig[0], int(fig[1:]))
    return (2, fig)
