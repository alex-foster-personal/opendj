"""Acceptance: vendored open-dj a4adf880 fixture flags B1 B2 B3 B5."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "quality" / "fixtures" / "open-dj-a4adf880"


def test_a4adf880_fixture_flags_seed_blockers() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.quality_rubric",
            "score",
            "--surface",
            "spec",
            "--root",
            str(FIXTURE),
            "--json",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["rubric_version"] == 1

    codes: set[str | None] = set()
    scores: dict[str, int] = {}
    for surface in report["surfaces"]:
        for dimension in surface["dimensions"]:
            scores[dimension["id"]] = dimension["score"]
            for finding in dimension["findings"]:
                codes.add(finding.get("code"))

    assert {"B1", "B2", "B3", "B5"}.issubset({c for c in codes if c})
    assert scores["xref.integrity"] <= 2
    assert scores["citation.resolves"] <= 3
    assert scores["schema.id_policy"] <= 2
    assert scores["source_of_truth.unique"] <= 2
