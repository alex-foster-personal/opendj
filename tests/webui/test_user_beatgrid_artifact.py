"""Own PQTZ artifact write/readback and quantize helper (issue #11140).

[if] a served own grid is stored as a PQTZ artifact [then] read-back and quantize use those beats, [else stop].
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from apps.shared.state import grid_edit
from apps.sync.analysis_writeback_pqtz import served_beats_from_own
from apps.webui.server.rb_vendor_pkg import anlz as anlz_mod
from apps.webui.server.rb_vendor_pkg.user_beatgrid_artifact import (
    quantize_against_artifact,
    read_served_beats_from_artifact,
    write_served_grid_artifact,
)

pytestmark = pytest.mark.requirement("BEATGRID-13512")

MEASURED = [{"n": (i % 4) + 1, "bpm": 128.0, "t": round(0.5 * i, 3)} for i in range(12)]


def _served_after_edit(beats: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "beatgrid": {"source": "own", "beats": beats, "beat_count": len(beats)},
        "tempo_changes": [],
    }
    edited = grid_edit.apply_to_payload(payload, grid_edit.make_edit(6.0, 1), "own")
    return edited["beatgrid"]


def test_edited_own_grid_artifact_round_trips_through_beatgrid_reader(tmp_path: Path) -> None:
    """[if] a fixed-tempo edited own grid is written then parsed [then] wire beats match, [else stop]."""
    served = _served_after_edit(MEASURED)
    path = tmp_path / "edited.DAT"
    write_served_grid_artifact(path, served)
    read = read_served_beats_from_artifact(path)
    from pyrekordbox.anlz import AnlzFile

    pqtz = AnlzFile.parse(path.read_bytes()).get_tag("PQTZ")
    overlay = anlz_mod._beatgrid_payload({"PQTZ": pqtz})[0]["beats"]
    assert read == overlay == served_beats_from_own(served["beats"])


def test_dynamic_tempo_payload_is_refused_at_artifact_write(tmp_path: Path) -> None:
    """[if] tempo_changes is non-empty at write [then] the writer refuses, [else stop]."""
    with pytest.raises(ValueError, match="tempo_changes"):
        write_served_grid_artifact(
            tmp_path / "dyn.DAT",
            {"beats": MEASURED, "tempo_changes": [{"at_s": 2.0, "bpm_before": 128, "bpm_after": 130}]},
        )


def test_invalid_beat_number_is_refused_at_encode(tmp_path: Path) -> None:
    """[if] beat n outside 1..4 is offered [then] encode refuses, [else stop]."""
    bad = [{**MEASURED[0], "n": 0}, *MEASURED[1:]]
    with pytest.raises(ValueError, match="1..4"):
        write_served_grid_artifact(tmp_path / "bad.DAT", {"beats": bad, "tempo_changes": []})


def test_quantize_against_artifact_snaps_to_read_back_times(tmp_path: Path) -> None:
    """[if] quantize uses read-back beats [then] the snap is a served beat t, [else stop]."""
    served = _served_after_edit(MEASURED)
    path = tmp_path / "snap.DAT"
    write_served_grid_artifact(path, served)
    snapped = quantize_against_artifact(path, 2.02)
    read = read_served_beats_from_artifact(path)
    assert snapped in {float(b["t"]) for b in read}
