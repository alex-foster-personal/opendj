"""Gate-to-genre validation for the four-stem genre bench (issue #13313).

Both content-gate JSON files must name only titles that ``MUSDB_TEST_GENRE``
declares, so an incomplete genre map fails in CI rather than after a Modal
benchmark has been paid for. Synthetic data only: no modal, GPU or audio.

-Claude
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from scripts.bench import four_stem_common as c

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_FILES: tuple[Path, ...] = (
    REPO_ROOT / "scripts" / "bench" / "stem_content_gate.json",
    REPO_ROOT / "scripts" / "bench" / "stem_content_gate_round2.json",
)
UNKNOWN_TITLE = "Nobody - Not A MUSDB Test Title"
# Families the spec sizes so a per-genre delta is reachable on the current gate set.
FAMILIES_WITH_N: tuple[str, ...] = ("electronic", "pop", "rock")


def _candidates(path: Path) -> list[dict[str, Any]]:
    """Return the candidate rows of one gate JSON file."""
    return json.loads(path.read_text())["candidates"]


def test_both_gate_files_exist_and_are_nonempty() -> None:
    """If a gate file went missing the coverage checks would pass vacuously."""
    for path in GATE_FILES:
        assert path.is_file(), path.name
        assert _candidates(path), path.name


@pytest.mark.parametrize("bad_index", [0, 1], ids=["round1", "round2"])
def test_unknown_title_in_either_gate_file_fails(tmp_path: Path, bad_index: int) -> None:
    """An unmapped title in either gate file raises before any cube is built."""
    paths: list[Path] = []
    for i, src in enumerate(GATE_FILES):
        doc = json.loads(src.read_text())
        if i == bad_index:
            extra = dict(doc["candidates"][0])
            extra["track"] = UNKNOWN_TITLE
            doc["candidates"].append(extra)
        dst = tmp_path / src.name
        dst.write_text(json.dumps(doc))
        paths.append(dst)
    flags = c.gate_flags(*paths)
    assert UNKNOWN_TITLE in flags
    with pytest.raises(RuntimeError, match="no coarse genre mapping") as exc:
        c.track_genres(flags)
    assert UNKNOWN_TITLE in str(exc.value)
    with pytest.raises(RuntimeError, match="no coarse genre mapping"):
        c.track_genres(c.iter_gate_candidate_tracks(*paths))


def test_sized_families_have_min_gated_tracks() -> None:
    """Electronic, pop and rock each carry at least MIN_GATED_PER_GENRE gated titles."""
    genres = c.track_genres(c.gate_flags(*GATE_FILES))
    counts = Counter(genres.values())
    for family in FAMILIES_WITH_N:
        assert counts[family] >= c.MIN_GATED_PER_GENRE, (family, counts)
