"""Listing rows carry the locally decoded strip when rekordbox has none (#4510).

A rekordbox-mapped row whose ANLZ preview is absent is decoded locally by the
single-track /anlz route, but the listing used to read only the ANLZ strip for
a mapped row, so the row showed a dash until it was selected. The strip is now
read from the local decode cache in that case, with no /anlz request.

  - [if] a mapped row has no ANLZ preview but a decoded local strip [then] the listing row carries it, [else stop].
"""

from __future__ import annotations

import base64
import math
import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.adapters.rekordbox.models import RbRowMeta
from apps.analysis_waveform import local_waveform
from apps.webui.server.rb_vendor_pkg import track_rows

pytestmark = [pytest.mark.requirement("PARITY-03"), pytest.mark.rb_parity]

SID = "a" * 40
RB_STRIP = ("cmVrb3JkYm94", 77)


def _meta(analysis_data_path: str | None) -> RbRowMeta:
    return RbRowMeta(
        vendor_id="42",
        folder_path="/Users/dev/Music/track.wav",
        analysis_data_path=analysis_data_path,
        comment=None,
        genre=None,
        play_count=0,
    )


def _write_wav(path: Path) -> None:
    rate = 8_000
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(20000 * math.sin(2 * math.pi * 60 * i / rate)))
                for i in range(rate)
            )
        )


@pytest.fixture
def decoded_locally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real local decode cache entry for SID, revalidated against real bytes."""
    monkeypatch.setattr(rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "cache")
    source = tmp_path / "source.wav"
    _write_wav(source)
    peaks = np.stack(
        [
            np.arange(600, dtype=np.uint8) % 200,
            np.full(600, 9, dtype=np.uint8),
            np.full(600, 30, dtype=np.uint8),
        ],
        axis=1,
    )
    local_waveform._store_peaks(SID, local_waveform._source_key(source), peaks)
    assert local_waveform.local_preview_strip(SID)[0] is not None


def test_mapped_row_without_anlz_preview_uses_the_local_strip(decoded_locally: None) -> None:
    preview_b64, preview_max = track_rows.row_preview_strip(SID, _meta(None))
    assert preview_b64 is not None, "a decoded local strip must reach the listing row"
    raw = base64.b64decode(preview_b64)
    assert len(raw) == 360, "120 cols x 3 bands, the same contract as the ANLZ strip"
    assert preview_max == max(raw)
    assert (preview_b64, preview_max) == local_waveform.local_preview_strip(SID)


def test_unmapped_row_still_uses_the_local_strip(decoded_locally: None) -> None:
    assert track_rows.row_preview_strip(SID, None) == local_waveform.local_preview_strip(SID)


def test_rekordbox_preview_wins_when_both_exist(
    decoded_locally: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []

    def fake_preview_strip(path: str | None, *, resolver: object = None) -> tuple[str, int]:
        seen.append(path)
        return RB_STRIP

    monkeypatch.setattr(track_rows, "preview_strip", fake_preview_strip)
    assert track_rows.row_preview_strip(SID, _meta("/PIONEER/USBANLZ/x/ANLZ0000.DAT")) == RB_STRIP
    assert seen == ["/PIONEER/USBANLZ/x/ANLZ0000.DAT"]


def test_neither_source_keeps_the_honest_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "empty-cache")
    assert track_rows.row_preview_strip(SID, _meta(None)) == (None, None)
    assert track_rows.row_preview_strip(SID, None) == (None, None)


def test_build_track_rows_routes_every_row_through_the_fallback() -> None:
    source = Path(track_rows.__file__).read_text(encoding="utf-8")
    body = source.split("def build_track_rows(", 1)[1].split("\ndef ", 1)[0]
    assert "row_preview_strip(" in body
    assert "local_preview_strip(" not in body, "the precedence lives in one helper"
