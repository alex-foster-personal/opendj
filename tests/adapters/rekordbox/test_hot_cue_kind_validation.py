"""Conservative ``djmdCue.Kind`` D-H handling in the rekordbox cue parsers (issue #13394).

Spec ``(private path)`` section 2 routes every reader
through ``cue_kind`` without changing the mapping: A-H stay Kind 1-8 until the
issue #4069 rekordbox-UI experiment records the real D-H integers. These tests
feed fixture ``djmdCue`` rows (no controller, no rekordbox database) through the
ingest parser (``_rb_cues_by_content``) and the read parser (``iter_cues``).

- [if] a cue kind maps to slot D [then] Kind 4 parses as hot cue D (not a loop, not E), [else stop].
- [if] a cue kind maps to slot H [then] Kind 8 parses as hot cue H, [else stop].
- [if] a reserved cue kind is encountered [then] Kind 9-11 is dropped, never guessed as a slot, [else stop].
- [if] an unknown cue kind is encountered [then] the row is dropped, never imported as a hot cue, [else stop].
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from apps.adapters.rekordbox.cue_kind import KIND_BY_SLOT
from apps.shared.rekordbox_db import iter_cues
from apps.shared.state.ingest import rekordbox as rb_ingest

pytestmark = pytest.mark.requirement("CUES-01")

CONTENT_ID = "4069"
RESERVED_KINDS: tuple[int, ...] = (9, 10, 11)
UNKNOWN_KINDS: tuple[int, ...] = (-1, 12, 99)


@dataclass
class CueRow:
    """Fixture ``djmdCue`` row with only the columns the parsers read."""

    Kind: int
    InMsec: int = 12_000
    OutMsec: int | None = None
    ActiveLoop: int = 0
    ContentID: str = CONTENT_ID
    rb_local_deleted: int = 0
    BeatLoopSize: int | None = None
    ColorTableIndex: int | None = None
    Color: int | None = None
    Comment: str | None = None


class FixtureDb:
    """Stand-in for ``Rekordbox6Database`` serving a fixed list of cue rows."""

    def __init__(self, rows: list[CueRow]) -> None:
        """Hold the fixture rows."""
        self.rows = rows

    def get_cue(self, ContentID: str | None = None) -> list[CueRow]:  # noqa: N803 - pyrekordbox kwarg
        """Return every row, or the rows for one ContentID."""
        if ContentID is None:
            return list(self.rows)
        return [row for row in self.rows if row.ContentID == ContentID]


def _ingest(rows: list[CueRow]) -> list[dict[str, Any]]:
    """Run the ingest parser and return the parsed cues for the fixture track."""
    return rb_ingest._rb_cues_by_content(FixtureDb(rows)).get(CONTENT_ID, [])


def _read(rows: list[CueRow]) -> list[tuple[str, int | None]]:
    """Run the read parser and return (kind, index) per parsed cue."""
    return [(cue.kind, cue.index) for cue in iter_cues(FixtureDb(rows), CONTENT_ID)]


def test_slot_d_kind_parses_as_hot_cue_d_on_ingest() -> None:
    """Kind 4 imports as hot cue D, not as a loop or a shifted E (conservative #4069 map)."""
    (cue,) = _ingest([CueRow(Kind=KIND_BY_SLOT["D"])])
    assert (cue["kind"], cue["slot"], cue["out_ms"]) == ("hot_cue", "D", None)


def test_slot_d_kind_parses_as_hot_index_three_on_read() -> None:
    """Kind 4 reads as hot cue index 3 (D), not the dead Kind-4-as-loop branch."""
    assert _read([CueRow(Kind=KIND_BY_SLOT["D"])]) == [("hot", 3)]


def test_slot_h_kind_parses_as_hot_cue_h_on_ingest() -> None:
    """Kind 8 imports as hot cue H, the last mapped slot."""
    (cue,) = _ingest([CueRow(Kind=KIND_BY_SLOT["H"])])
    assert (cue["kind"], cue["slot"]) == ("hot_cue", "H")


def test_slot_h_kind_parses_as_hot_index_seven_on_read() -> None:
    """Kind 8 reads as hot cue index 7 (H)."""
    assert _read([CueRow(Kind=KIND_BY_SLOT["H"])]) == [("hot", 7)]


def test_d_through_h_keep_their_letters_in_one_track() -> None:
    """A track carrying Kind 4-8 imports D-H in order with no letter shifted by one."""
    rows = [CueRow(Kind=KIND_BY_SLOT[slot], InMsec=1_000 * i) for i, slot in enumerate("DEFGH")]
    assert [cue["slot"] for cue in _ingest(rows)] == list("DEFGH")
    assert _read(rows) == [("hot", i) for i in range(3, 8)]


@pytest.mark.parametrize("kind", RESERVED_KINDS)
def test_reserved_kind_is_dropped_by_both_parsers(kind: int) -> None:
    """Kind 9-11 is unverified (issue #263, #4069) and must never surface as a hot cue."""
    rows = [CueRow(Kind=kind)]
    assert _ingest(rows) == []
    assert _read(rows) == []


@pytest.mark.parametrize("kind", UNKNOWN_KINDS)
def test_unknown_kind_is_dropped_by_both_parsers(kind: int) -> None:
    """An out-of-range Kind is skipped rather than mapped to a guessed slot."""
    rows = [CueRow(Kind=kind)]
    assert _ingest(rows) == []
    assert _read(rows) == []


def test_reserved_and_unknown_rows_do_not_disturb_mapped_neighbours() -> None:
    """Dropped rows leave the D and H cues on the same track intact."""
    rows = [
        CueRow(Kind=KIND_BY_SLOT["D"], InMsec=1_000),
        CueRow(Kind=9, InMsec=2_000),
        CueRow(Kind=99, InMsec=3_000),
        CueRow(Kind=KIND_BY_SLOT["H"], InMsec=4_000),
    ]
    assert [(cue["slot"], cue["in_ms"]) for cue in _ingest(rows)] == [("D", 1_000), ("H", 4_000)]
    assert _read(rows) == [("hot", 3), ("hot", 7)]
