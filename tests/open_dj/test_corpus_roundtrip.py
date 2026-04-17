"""Round-trip tests for the v0.2 conformance corpus.

Every file under ``open-dj/conformance/corpus-0.2/`` must:

1. Be byte-stable under :func:`apps.open_dj.canon.to_canonical_bytes`
   (i.e. the committed bytes already are canonical).
2. Validate clean against the v0.2 JSON Schema.
3. Idempotently round-trip: ``canon(parse(canon(x))) == canon(x)``.

This is the strawman section 0.2 gate: "10-track seed corpus round-trips".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.open_dj.canon import to_canonical_bytes
from apps.open_dj.validate import validate_document

CORPUS_DIR: Path = Path(__file__).resolve().parents[2] / "open-dj" / "conformance" / "corpus-0.2"


def _corpus_files() -> list[Path]:
    """Return every ``*.open-dj.json`` in the corpus directory."""
    return sorted(CORPUS_DIR.glob("*.open-dj.json"))


CORPUS_PARAMS = [
    pytest.param(p, id=p.stem) for p in _corpus_files()
]


@pytest.mark.requirement("OPEN-01")
def test_corpus_is_not_empty() -> None:
    """Guard against accidentally shipping no corpus."""
    files = _corpus_files()
    assert len(files) >= 10, (
        f"corpus-0.2 must have at least 10 cases; found {len(files)}"
    )


@pytest.mark.requirement("OPEN-02")
@pytest.mark.parametrize("corpus_file", CORPUS_PARAMS)
def test_corpus_canon_stable(corpus_file: Path) -> None:
    """Raw bytes are already canonical -- re-emitting yields same bytes."""
    raw = corpus_file.read_bytes()
    doc = json.loads(raw)
    assert to_canonical_bytes(doc) == raw, (
        f"{corpus_file.name} is not canonical; run `python -m apps.open_dj.cli "
        f"canon --in-place {corpus_file}` and commit."
    )


@pytest.mark.requirement("OPEN-02")
@pytest.mark.parametrize("corpus_file", CORPUS_PARAMS)
def test_corpus_canon_idempotent(corpus_file: Path) -> None:
    """canon(parse(canon(x))) must equal canon(x)."""
    raw = corpus_file.read_bytes()
    doc = json.loads(raw)
    once = to_canonical_bytes(doc)
    twice = to_canonical_bytes(json.loads(once))
    assert once == twice


@pytest.mark.requirement("OPEN-02")
@pytest.mark.parametrize("corpus_file", CORPUS_PARAMS)
def test_corpus_validates(corpus_file: Path) -> None:
    """Every corpus file validates against the v0.2 schema."""
    doc = json.loads(corpus_file.read_text(encoding="utf-8"))
    errors = validate_document(doc)
    assert errors == [], f"{corpus_file.name}: {errors}"


@pytest.mark.requirement("OPEN-01")
@pytest.mark.parametrize("corpus_file", CORPUS_PARAMS)
def test_corpus_has_schema_version_0_2(corpus_file: Path) -> None:
    """Every corpus file declares schema_version == 0.2."""
    doc = json.loads(corpus_file.read_text(encoding="utf-8"))
    assert doc["schema_version"] == "0.2"


@pytest.mark.requirement("OPEN-01")
def test_case_01_classic_isrc_track_id_pinned() -> None:
    """case-01 is a regression-pin for the canonical ISRC -> sha1 path."""
    doc = json.loads(
        (CORPUS_DIR / "case-01-classic-isrc.open-dj.json").read_text("utf-8")
    )
    track = doc["tracks"][0]
    assert track["track_id"] == "9bbb11637465090ce8135bcc3e66c0f00cf777fa"
    assert track["isrc"] == "GBCEN0900132"


@pytest.mark.requirement("OPEN-01")
def test_case_13_has_two_play_orders() -> None:
    """Spec section 7.4: named play-orders are corpus case 13."""
    doc = json.loads(
        (CORPUS_DIR / "case-13-play-orders.open-dj.json").read_text("utf-8")
    )
    pl = doc["playlists"][0]
    orders = pl.get("play_orders", [])
    assert len(orders) == 2
    names = {o["name"] for o in orders}
    assert names == {"warm-up", "peak-time"}
