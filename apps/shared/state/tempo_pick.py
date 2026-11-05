"""The user's pick between our tempo and Mixed In Key's for one track (GRID-OCTAVE-02).

Where our beat analysis and Mixed In Key (MIK) disagree on a track's tempo
(an octave apart, or more than 6% apart; `apps.analysis_beatgrid.mik_grid`),
the deck serves ours and marks it disputed. One click in the BPM hover card,
``PUT /api/v1/tracks/{id}/bpm-pick`` or ``opendj bpm pick`` stores the user's
choice as the provenance-wrapped ``track_fields`` row ``grid_tempo_pick``
(source ``webui``), beside the ``beatgrid_edit`` row, so it survives restart,
keeps history and travels with CloudSync like every other user field. `/anlz`
and the library row both read it: "mik" serves MIK's grid and tempo, "own"
keeps ours with the dispute settled. Clearing it writes ``{"source": null}``.

This module owns no connection and opens no transaction.

-Claude
"""

from __future__ import annotations

import json
import sqlite3
from typing import Final, Literal, get_args

FIELD: Final = "grid_tempo_pick"
BLOB_VERSION: Final = 1
OWN_SOURCE: Final = "webui"

TempoPick = Literal["own", "mik"]
TEMPO_PICKS: Final[tuple[TempoPick, ...]] = get_args(TempoPick)


def blob(pick: TempoPick | None) -> dict[str, object]:
    return {"v": BLOB_VERSION, "source": pick}


def read(conn: sqlite3.Connection, stable_id: str) -> TempoPick | None:
    """The stored pick; None when there is none, the table is absent, or it was cleared.

    Raises ValueError for a stored value this version cannot read, rather than
    serving a grid the user did not choose.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_fields'").fetchone():
        return None
    row = conn.execute(
        "SELECT value_json FROM track_fields WHERE stable_id = ? AND field_name = ? AND deleted_at IS NULL",
        (stable_id, FIELD),
    ).fetchone()
    if row is None:
        return None
    value = json.loads(str(row[0]))
    pick = value.get("source") if isinstance(value, dict) else None
    if not (isinstance(value, dict) and value.get("v") == BLOB_VERSION) or (pick is not None and pick not in TEMPO_PICKS):
        raise ValueError(f"{FIELD} for {stable_id} is not a v{BLOB_VERSION} pick: {row[0]!r}")
    return pick


__all__ = ["BLOB_VERSION", "FIELD", "OWN_SOURCE", "TEMPO_PICKS", "TempoPick", "blob", "read"]
