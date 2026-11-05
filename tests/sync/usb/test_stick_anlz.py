"""Stick track analysis read straight off a rekordbox USB stick (USBPLAY-07).

Requirements:

✔︎ Each track's analysis comes from its OWN analyze_path file, never a directory scan.
✔︎ One tag that will not parse costs one lane, and is listed in unreadable_anlz.
✔︎ PCO2 (.EXT) cues win over PCOB (.DAT); PCOB only fills a list PCO2 cannot.
✔︎ Every file must resolve under <volume>/PIONEER/USBANLZ.
✔︎ The payload is the library /anlz shape (build_anlz_payload) minus stable_id/points
  (held by (private path), which reads the web UI's builder).

Acceptance tests (single-line intent):

  - if two tracks share one ANLZ directory and one gets the other's grid then broken
  - if a corrupt PCO2 tag drops the PQTZ grid or goes unrecorded then broken
  - if PCOB cues are served when PCO2 decodes then broken
  - if a 44-byte PCP2 entry (no color bytes) loses its hot cue then broken
  - if an analyze_path escaping PIONEER/USBANLZ is read then broken
  - if the payload's keys, waveform, grid or phrases drift from build_anlz_payload then broken
  - if a hot cue does not validate as the library AnlzCueOut then broken
  - if the live stick's ANLZ0001 track in P016/00024756 gets ANLZ0000's grid then broken

Every ANLZ file here is SYNTHETIC, built byte by byte below. Nothing is copied
from a real stick (the repo is public). The live test reads the stick named by
MDT_USB_STICK_ROOT, read only, and fails closed when it is unset (see live_stick.py).

-Claude
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import pytest
from construct.core import ConstructError
from pyrekordbox.anlz import AnlzFile

from apps.sync.usb.pioneer.anlz_track import (
    StickAnlzFileMissing,
    StickAnlzPathRefused,
    StickAnlzUnreadable,
    read_stick_track_analysis,
)
from tests.sync.usb.anlz_bytes import (
    grid,
    pco2,
    pcob,
    pcp2,
    pcpt,
    pmai,
    pqtz,
    pvdi,
    pwav,
    pwv3,
    pwv5,
    pwv6,
    pwv7,
    write_track,
)

POINTS = 256
SHARED_DIR = "PIONEER/USBANLZ/P016/00024756"


def _read(volume: Path, analyze_path: str, points: int = POINTS) -> Any:
    return read_stick_track_analysis(volume_root=volume, analyze_path=analyze_path, points=points)


# ----- exact files, shared directory -------------------------------------------


def test_two_tracks_in_one_anlz_directory_each_get_their_own_analysis(tmp_path: Path) -> None:
    first = write_track(
        tmp_path,
        f"{SHARED_DIR}/ANLZ0000.DAT",
        dat=pmai(pqtz(grid(152, 124.0, 16))),
        ext=pmai(pco2(1, pcp2(1, 152, color=43)), pco2(0)),
        twoex=pmai(pwv6(120, 1), pwv7(600, 1)),
    )
    second = write_track(
        tmp_path,
        f"{SHARED_DIR}/ANLZ0001.DAT",
        dat=pmai(pqtz(grid(19, 132.0, 12))),
        ext=pmai(pco2(1, pcp2(5, 19, color=19)), pco2(0)),
        twoex=pmai(pwv6(120, 2), pwv7(600, 2)),
    )

    a, b = _read(tmp_path, first), _read(tmp_path, second)

    assert (a.payload["beatgrid"]["beat_count"], a.payload["beatgrid"]["beats"][0]) == (
        16,
        {"n": 1, "bpm": 124.0, "t": 0.152},
    ), "if ANLZ0000 does not get its own grid then exact-file resolution is broken"
    assert (b.payload["beatgrid"]["beat_count"], b.payload["beatgrid"]["beats"][0]) == (
        12,
        {"n": 1, "bpm": 132.0, "t": 0.019},
    ), "if ANLZ0001 gets ANLZ0000's grid then the shared-directory case is broken"
    assert [(c["slot"], c["position_ms"], c["color"]) for c in a.hot_cues] == [(0, 152, 43)]
    assert [(c["slot"], c["position_ms"], c["color"]) for c in b.hot_cues] == [(4, 19, 19)]
    assert a.payload["waveform"] != b.payload["waveform"], (
        "if both tracks get one waveform then the .2EX sibling is not the track's own"
    )


# ----- one bad tag, one lane -----------------------------------------------------


def test_corrupt_pco2_keeps_the_grid_falls_back_to_pcob_and_is_recorded(tmp_path: Path) -> None:
    broken = bytearray(pco2(1, pcp2(1, 2000, color=43)))
    struct.pack_into(">I", broken, 20 + 8, 4096)  # PCP2 len_entry overruns its tag
    path = write_track(
        tmp_path,
        "PIONEER/USBANLZ/P001/0000000A/ANLZ0000.DAT",
        dat=pmai(pqtz(grid(100, 120.0, 8)), pcob(1, pcpt(1, 1500)), pcob(0)),
        ext=pmai(bytes(broken), pco2(0), pcob(1), pcob(0)),
    )

    result = _read(tmp_path, path)

    assert result.payload["beatgrid"]["beat_count"] == 8, (
        "if a corrupt PCO2 tag drops the PQTZ grid then per-tag isolation is broken"
    )
    unreadable = result.payload["unreadable_anlz"]
    assert len(unreadable) == 1 and unreadable[0].startswith("ANLZ0000.EXT:PCO2: ValueError"), (
        f"if the PCO2 failure is not recorded as file:fourcc: error then it is silent: {unreadable}"
    )
    assert [(c["slot"], c["position_ms"], c["color"]) for c in result.hot_cues] == [(0, 1500, None)], (
        "if the hot list does not fall back to .DAT PCOB when PCO2 is unreadable then cues are lost"
    )


def test_pco2_is_preferred_over_pcob(tmp_path: Path) -> None:
    path = write_track(
        tmp_path,
        "PIONEER/USBANLZ/P001/0000000B/ANLZ0000.DAT",
        dat=pmai(pqtz(grid(0, 128.0, 4)), pcob(1, pcpt(1, 1000)), pcob(0, pcpt(0, 500))),
        ext=pmai(
            pco2(1, pcp2(1, 2000, comment="drop", color=43), pcp2(8, 9000, color=5, loop_ms=11000)),
            pco2(0, pcp2(0, 750, comment="intro", color=0)),
        ),
    )

    result = _read(tmp_path, path)

    assert result.hot_cues == [
        {"slot": 0, "position_ms": 2000, "color": 43, "label": "drop"},
        {"slot": 7, "position_ms": 9000, "color": 5, "label": None},
    ], "if PCOB positions or a lost color/label are served while PCO2 decodes then it is broken"
    assert result.memory_cues == [{"position_ms": 750, "color": 0, "label": "intro"}]
    by_slot = {c["slot"]: c for c in result.payload["cues"]}
    assert (by_slot["H"]["is_loop"], by_slot["H"]["out_ms"]) == (True, 11000)
    assert [c["in_ms"] for c in result.payload["cues"]] == [750, 2000, 9000], "cues sort by in_ms"
    assert result.payload["unreadable_anlz"] == []


def test_a_44_byte_pcp2_entry_keeps_its_hot_cue_without_a_color(tmp_path: Path) -> None:
    ext = pmai(pco2(1, pcp2(5, 24)), pco2(0))
    with pytest.raises(ConstructError):  # control: a whole-file pyrekordbox parse fails on it
        AnlzFile.parse(ext)
    path = write_track(
        tmp_path,
        "PIONEER/USBANLZ/P001/0000000C/ANLZ0000.DAT",
        dat=pmai(pqtz(grid(24, 124.0, 4))),
        ext=ext,
    )

    result = _read(tmp_path, path)

    assert result.hot_cues == [{"slot": 4, "position_ms": 24, "color": None, "label": None}], (
        "if a 44-byte PCP2 entry loses hot cue E then the raw PCO2 decode is broken"
    )
    assert result.payload["unreadable_anlz"] == []


def test_mono_detail_prefers_pwv5_and_an_unreadable_pwv5_falls_to_pwv3(tmp_path: Path) -> None:
    good = write_track(
        tmp_path,
        "PIONEER/USBANLZ/P001/0000000D/ANLZ0000.DAT",
        dat=pmai(pqtz(grid(0, 120.0, 4)), pwav(400, 3)),
        ext=pmai(pwv3(300, 4), pwv5([31, 0, 16] * 100)),
    )
    bad_pwv5 = bytearray(pwv5([31, 0, 16] * 100))
    struct.pack_into(">I", bad_pwv5, 12, 6)  # entry size != 2
    fallback = write_track(
        tmp_path,
        "PIONEER/USBANLZ/P001/0000000E/ANLZ0000.DAT",
        dat=pmai(pqtz(grid(0, 120.0, 4)), pwav(400, 3)),
        ext=pmai(pwv3(300, 4), bytes(bad_pwv5)),
    )

    mono = _read(tmp_path, good, points=1000).payload
    assert mono["waveform"]["kind"] == "mono"
    assert mono["waveform"]["detail"]["low"][:3] == [1.0, 0.0, round(16 / 31, 4)], (
        "if the mono detail is not the PWV5 height lane then the fallback order is broken"
    )
    fell_back = _read(tmp_path, fallback, points=1000).payload
    assert fell_back["waveform"]["detail"]["length"] == 300, "PWV3 serves the detail when PWV5 fails"
    assert fell_back["unreadable_anlz"][0].startswith("ANLZ0000.EXT:PWV5: ValueError")


def test_a_pvdi_tag_is_listed_not_silently_reported_as_no_analysis(tmp_path: Path) -> None:
    path = write_track(
        tmp_path,
        "PIONEER/USBANLZ/P001/0000000F/ANLZ0000.DAT",
        dat=pmai(pqtz(grid(0, 120.0, 4))),
        twoex=pmai(pwv6(120, 1), pwv7(600, 1), pvdi(bytes([0, 2, 2, 0]))),
    )

    payload = _read(tmp_path, path).payload

    assert payload["vocals"] == {"status": "not_analyzed"}
    assert payload["unreadable_anlz"] == [
        "ANLZ0000.2EX:PVDI: vocal regions are not decoded for stick tracks; vocals report not_analyzed"
    ], "if an undecoded PVDI is not listed then not_analyzed hides real vocal data"


def test_a_track_with_no_readable_tag_raises_analysis_not_found(tmp_path: Path) -> None:
    path = write_track(tmp_path, "PIONEER/USBANLZ/P001/00000010/ANLZ0000.DAT", dat=b"not anlz")
    with pytest.raises(StickAnlzUnreadable) as excinfo:
        _read(tmp_path, path)
    assert excinfo.value.code == "ANALYSIS_NOT_FOUND"


# ----- containment -------------------------------------------------------------


def test_paths_escaping_usbanlz_are_refused(tmp_path: Path) -> None:
    volume = tmp_path / "STICK"
    outside = write_track(volume, "PIONEER/Elsewhere/ANLZ0000.DAT", dat=pmai(pqtz(grid(0, 120.0, 4))))
    for analyze_path in (
        outside,
        "/PIONEER/USBANLZ/../Elsewhere/ANLZ0000.DAT",
        "/PIONEER/USBANLZ/P001/../../../../etc/ANLZ0000.DAT",
        "/PIONEER/USBANLZ/P001/00000011/ANLZ0000.EXT",
    ):
        with pytest.raises(StickAnlzPathRefused) as excinfo:
            _read(volume, analyze_path)
        assert excinfo.value.code == "USB_PATH_OUTSIDE_VOLUME", analyze_path

    linked = write_track(volume, "PIONEER/USBANLZ/P001/00000012/ANLZ0000.DAT", dat=pmai(pqtz(grid(0, 120.0, 4))))
    (volume / linked.lstrip("/")).with_suffix(".EXT").symlink_to(volume / outside.lstrip("/"))
    with pytest.raises(StickAnlzPathRefused):
        _read(volume, linked)


def test_missing_analysis_is_a_typed_file_missing(tmp_path: Path) -> None:
    for analyze_path in ("", "/PIONEER/USBANLZ/P001/00000013/ANLZ0000.DAT"):
        with pytest.raises(StickAnlzFileMissing) as excinfo:
            _read(tmp_path, analyze_path)
        assert excinfo.value.code == "USB_FILE_MISSING"
