"""File tags become a proposal, never a write (PRD R-CLS-4)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.shared import classical_tags


def _tag(composer: str | None = None, year: object = None, **other: list[str]) -> SimpleNamespace:
    return SimpleNamespace(composer=composer, year=year, other=other)


def _frame(name: bytes, text: str) -> bytes:
    body = b"\x03" + text.encode()
    return name + bytes((0, 0, 0, len(body))) + b"\x00\x00" + body


def _id3_file(path: Path, *frames: bytes) -> Path:
    body = b"".join(frames)
    size = len(body)
    header = b"ID3\x04\x00\x00" + bytes(((size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F))
    path.write_bytes(header + body + b"\x00" * 64)
    return path


def test_a_real_id3_file_is_read(tmp_path: Path) -> None:
    path = _id3_file(
        tmp_path / "a.mp3",
        _frame(b"TCOM", "Mozart"),
        _frame(b"TPUB", "Deutsche Grammophon"),
        _frame(b"TPE3", "Karajan"),
        _frame(b"MVNM", "Lacrimosa"),
        _frame(b"MVIN", "8/14"),
        _frame(b"TIT1", "Requiem"),
    )
    assert classical_tags.read_proposal(path) == {
        "work": "Requiem",
        "composer": "Mozart",
        "movement": "Lacrimosa",
        "movement_number": 8,
        "label": "Deutsche Grammophon",
        "conductor": "Karajan",
    }


@pytest.mark.parametrize(("raw", "number"), [("1/", 1), ("3/12", 3), ("/", None), ("x", None)])
def test_a_malformed_movement_number_never_crashes(raw: str, number: int | None) -> None:
    proposal = classical_tags.proposal_from_tag(_tag(movement=[raw]))
    assert proposal.get("movement_number") == number


def test_an_untagged_file_proposes_nothing() -> None:
    assert classical_tags.proposal_from_tag(_tag()) == {}


def test_year_comes_from_a_full_date() -> None:
    assert classical_tags.proposal_from_tag(_tag(year="1962-03-01"))["recording_year"] == 1962
