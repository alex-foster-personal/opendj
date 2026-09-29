"""DATASETS-01: the labelled-datasets register is complete and well formed.

`docs/datasets.md` is the one list of labelled datasets we use, are weighing, or
rejected. Two things keep it honest:

  * Every row carries the fields a reader needs to judge a number measured on it:
    who made the labels, how much EDM it holds, both licenses, and a status from
    a fixed set.
  * Bench and analysis code cannot name a known public dataset that has no row.
    WATCHLIST is the set of names we know to look for. A hit on a name that no
    row claims fails, naming the file.

The scan proves it can find something before its silence counts: GTZAN is
wired into `scripts/beatbench`, so the scan must report it, and a name planted
in a temporary file must be reported as unregistered.

-Claude
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("DATASETS-01")

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER = REPO_ROOT / "docs" / "datasets.md"

#: Where corpora get wired in. Docs and research notes are not scanned: they
#: discuss datasets we never run, which is what the register is for.
SCANNED_DIRS = (
    "apps/analysis_bench",
    "apps/analysis_beatgrid",
    "apps/analysis_key",
    "scripts/beatbench",
    "scripts/bench",
    "scripts/keybench",
    "scripts/loudnessbench",
)
SCANNED_SUFFIXES = {".py", ".sh", ".json", ".md", ".html", ".yaml", ".yml"}

#: Public dataset names worth catching. Add a name here when a new corpus
#: becomes plausible; the register must then carry it before code may use it.
WATCHLIST = (
    "GTZAN",
    "GiantSteps",
    "GiantSteps+",
    "GiantSteps-MTG",
    "Raveform",
    "Harmonix",
    "HJDB",
    "Ballroom",
    "SMC",
    "Isophonics",
    "RWC",
    "Billboard",
    "KeyFinder v2",
    "EDM-CUE",
    "M-DJCUE",
    "SALAMI",
    "SongFormBench",
    "MUSDB18",
    "Slakh",
    "MTG-Jamendo",
    "JamendoLyrics",
)

REQUIRED_COLUMNS = (
    "id",
    "name",
    "aliases",
    "lanes",
    "labels",
    "edm",
    "size",
    "license (annotations / audio)",
    "status",
    "notes and where it is used",
)
STATUSES = {"used", "candidate", "rejected"}
EDM_VALUES = {"yes", "part", "no"}


def _rows() -> list[dict[str, str]]:
    lines = REGISTER.read_text(encoding="utf-8").splitlines()
    start = lines.index("## Register")
    table = [ln for ln in lines[start:] if ln.startswith("|")]
    header = [c.strip() for c in table[0].strip("|").split("|")]
    assert tuple(header) == REQUIRED_COLUMNS, header
    rows = []
    for ln in table[2:]:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        assert len(cells) == len(header), ln
        rows.append(dict(zip(header, cells, strict=True)))
    return rows


def _alias_pattern(name: str) -> re.Pattern[str]:
    # Whole-word, case-insensitive. A trailing '+' is part of a name
    # (GiantSteps+), so the right edge also refuses '+'. A trailing '-' is
    # allowed: MUSDB18-HQ is a MUSDB18 variant and must still be caught. '_'
    # is a boundary too, so a code identifier like gtzan_corpus counts.
    return re.compile(rf"(?<![A-Za-z0-9-]){re.escape(name)}(?![A-Za-z0-9+])", re.IGNORECASE)


def _registered_aliases() -> set[str]:
    aliases: set[str] = set()
    for row in _rows():
        aliases.update(a.strip().lower() for a in row["aliases"].split(",") if a.strip())
    return aliases


def _scan(roots: list[Path]) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    patterns = {name: _alias_pattern(name) for name in WATCHLIST}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for name, pattern in patterns.items():
                if pattern.search(text):
                    hits.setdefault(name, []).append(str(path.relative_to(REPO_ROOT)
                                                         if path.is_relative_to(REPO_ROOT)
                                                         else path))
    return hits


def _missing(hits: dict[str, list[str]], registered: set[str]) -> dict[str, list[str]]:
    return {name: files for name, files in hits.items() if name.lower() not in registered}


def test_register_rows_are_complete() -> None:
    rows = _rows()
    assert len(rows) >= 20, f"register shrank to {len(rows)} rows"
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate dataset id"
    for row in rows:
        for column in REQUIRED_COLUMNS:
            assert row[column], f"{row['id']}: empty {column!r}"
        assert row["status"] in STATUSES, f"{row['id']}: status {row['status']!r}"
        assert row["edm"] in EDM_VALUES, f"{row['id']}: edm {row['edm']!r}"
        assert " / " in row["license (annotations / audio)"], (
            f"{row['id']}: license must name annotations and audio separately"
        )


def test_rejected_rows_say_why() -> None:
    for row in _rows():
        if row["status"] == "rejected":
            assert len(row["notes and where it is used"]) >= 20, row["id"]


def test_every_watched_name_in_code_is_registered() -> None:
    roots = [REPO_ROOT / d for d in SCANNED_DIRS if (REPO_ROOT / d).is_dir()]
    hits = _scan(roots)
    # Positive control: GTZAN is wired into scripts/beatbench, so a scan that
    # cannot see it is not measuring anything.
    assert "scripts/beatbench/gtzan_corpus.py" in hits.get("GTZAN", []), (
        "scan did not report GTZAN in scripts/beatbench/gtzan_corpus.py; the scan is broken"
    )
    missing = _missing(hits, _registered_aliases())
    assert not missing, f"datasets named in code but absent from docs/datasets.md: {missing}"


def test_watchlist_is_fully_registered() -> None:
    registered = _registered_aliases()
    absent = [name for name in WATCHLIST if name.lower() not in registered]
    assert not absent, f"watchlist names with no register row: {absent}"


def test_scan_reports_an_unregistered_name(tmp_path: Path) -> None:
    # Negative control: a watched name the register does not claim must be
    # reported, loudly, rather than passing as an empty result.
    (tmp_path / "bench.py").write_text("CORPUS = 'giantsteps+ excerpts'\n")
    (tmp_path / "other.py").write_text("GIANTSTEPSX = 1  # longer identifier, no hit\n")
    hits = _scan([tmp_path])
    assert set(hits) == {"GiantSteps+"}, hits
    registered = _registered_aliases()
    assert "giantsteps+" in registered
    assert set(_missing(hits, registered - {"giantsteps+"})) == {"GiantSteps+"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("scored on GTZAN", True),
        ("gtzan_corpus.py", True),
        ("GTZANX", False),
        ("MUSDB18-HQ test track", True),
        ("MUSDB18X", False),
    ],
)
def test_alias_matching_is_whole_word(text: str, expected: bool) -> None:
    name = "MUSDB18" if "MUSDB" in text else "GTZAN"
    assert bool(_alias_pattern(name).search(text)) is expected
