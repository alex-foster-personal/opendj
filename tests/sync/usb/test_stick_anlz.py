"""Stick track analysis read straight off a rekordbox USB stick (USBPLAY-07).

Requirements:

✔︎ Each track's analysis comes from its OWN analyze_path file, never a directory scan.
✔︎ One tag that will not parse costs one lane, and is listed in unreadable_anlz.
✔︎ PCO2 (.EXT) cues win over PCOB (.DAT); PCOB only fills a list PCO2 cannot.
✔︎ Every file must resolve under <volume>/PIONEER/USBANLZ.
✔︎ The payload is the library /anlz shape (build_anlz_payload) minus stable_id/points.

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
MDT_USB_STICK_ROOT, read only, and skips with a stated reason when it is unset.

-Claude
"""

from __future__ import annotations

import os
import statistics
import struct
import time
from pathlib import Path
from typing import Any

import pytest
from construct.core import ConstructError
from pyrekordbox.anlz import AnlzFile

from apps.sync.usb.pioneer.anlz_track import (
    HOT_CUE_SLOTS,
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
    pssi,
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
    assert [(c["slot"], c["position_ms"], c["color"]) for c in result.hot_cues] == [
        (0, 1500, None)
    ], "if the hot list does not fall back to .DAT PCOB when PCO2 is unreadable then cues are lost"


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
    assert fell_back["waveform"]["detail"]["length"] == 300, (
        "PWV3 serves the detail when PWV5 fails"
    )
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
        "ANLZ0000.2EX:PVDI: vocal regions are not decoded for stick tracks; "
        "vocals report not_analyzed"
    ], "if an undecoded PVDI is not listed then not_analyzed hides real vocal data"


def test_a_track_with_no_readable_tag_raises_analysis_not_found(tmp_path: Path) -> None:
    path = write_track(tmp_path, "PIONEER/USBANLZ/P001/00000010/ANLZ0000.DAT", dat=b"not anlz")
    with pytest.raises(StickAnlzUnreadable) as excinfo:
        _read(tmp_path, path)
    assert excinfo.value.code == "ANALYSIS_NOT_FOUND"


# ----- containment -------------------------------------------------------------


def test_paths_escaping_usbanlz_are_refused(tmp_path: Path) -> None:
    volume = tmp_path / "STICK"
    outside = write_track(
        volume, "PIONEER/Elsewhere/ANLZ0000.DAT", dat=pmai(pqtz(grid(0, 120.0, 4)))
    )
    for analyze_path in (
        outside,
        "/PIONEER/USBANLZ/../Elsewhere/ANLZ0000.DAT",
        "/PIONEER/USBANLZ/P001/../../../../etc/ANLZ0000.DAT",
        "/PIONEER/USBANLZ/P001/00000011/ANLZ0000.EXT",
    ):
        with pytest.raises(StickAnlzPathRefused) as excinfo:
            _read(volume, analyze_path)
        assert excinfo.value.code == "USB_PATH_OUTSIDE_VOLUME", analyze_path

    linked = write_track(
        volume, "PIONEER/USBANLZ/P001/00000012/ANLZ0000.DAT", dat=pmai(pqtz(grid(0, 120.0, 4)))
    )
    (volume / linked.lstrip("/")).with_suffix(".EXT").symlink_to(volume / outside.lstrip("/"))
    with pytest.raises(StickAnlzPathRefused):
        _read(volume, linked)


def test_missing_analysis_is_a_typed_file_missing(tmp_path: Path) -> None:
    for analyze_path in ("", "/PIONEER/USBANLZ/P001/00000013/ANLZ0000.DAT"):
        with pytest.raises(StickAnlzFileMissing) as excinfo:
            _read(tmp_path, analyze_path)
        assert excinfo.value.code == "USB_FILE_MISSING"


# ----- parity with the library builder ------------------------------------------


def _library_payload(monkeypatch: pytest.MonkeyPatch, dat: Path, points: int) -> dict[str, Any]:
    """The REAL build_anlz_payload over the same files, its I/O seams pinned."""
    from apps.adapters.rekordbox import paths as rb_paths
    from apps.shared import platform_paths as pp
    from apps.webui.server import rb_vendor
    from apps.webui.server.rb_vendor_pkg import anlz as rb_anlz
    from apps.webui.server.rb_vendor_pkg import anlz_cache, beatgrid_issue_cache
    from apps.webui.server.rb_vendor_pkg import db as rb_db

    monkeypatch.setattr(rb_paths, "anlz_dir", lambda _content: dat.parent)
    monkeypatch.setattr(
        rb_paths,
        "resolve_asset_path",
        lambda original: pp.MappedPath(
            original=original, resolved=dat, mapped=True, reason="native"
        ),
    )
    monkeypatch.setattr(
        rb_paths,
        "_asset_sibling",
        lambda mapped, candidate: pp.MappedPath(
            original=mapped.original, resolved=candidate, mapped=True, reason="native"
        ),
    )
    monkeypatch.setattr(anlz_cache, "_anlz_mtime", lambda _directory: 1.0)
    monkeypatch.setattr(anlz_cache, "_load_cached_payload", lambda *_args: None)
    monkeypatch.setattr(anlz_cache, "_store_cached_payload", lambda *_args: None)
    monkeypatch.setattr(beatgrid_issue_cache, "_ensure_beatgrid_issue_cached", lambda *_args: None)
    monkeypatch.setattr(rb_db, "fetch_cues", lambda _vendor_id: [])
    monkeypatch.setattr(rb_anlz, "apply_own_overlays", lambda payload, *_args: payload)
    content = rb_vendor.RbContent(
        stable_id="parity",
        vendor_id="v",
        folder_path=None,
        image_path=None,
        analysis_data_path=str(dat),
        length_s=None,
        comment=None,
        genre=None,
    )
    return rb_vendor.build_anlz_payload(content, points)


@pytest.mark.parametrize("kind", ["tri", "mono"])
def test_payload_matches_the_library_builder(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    masked = pssi([(1, 1), (17, 5), (33, 2)], end_beat=48, masked=True)
    assert not 1 <= int.from_bytes(masked[18:20], "big") <= 3, (
        "control: the masked PSSI must read as garbled, or the XOR path is not exercised"
    )
    rel = f"PIONEER/USBANLZ/P002/{kind.upper():0>8}/ANLZ0000.DAT"
    path = write_track(
        tmp_path,
        rel,
        dat=pmai(pqtz(grid(351, 90.0, 48)), pwav(400, 5), pcob(1), pcob(0)),
        ext=pmai(pwv3(1800, 6), pcob(1), pcob(0), pco2(1, pcp2(1, 351, color=43)), pco2(0), masked),
        twoex=pmai(pwv6(1200, 7), pwv7(1800, 7)) if kind == "tri" else None,
    )
    points = 700  # below the detail length, so downsampling is compared too

    ours = _read(tmp_path, path, points=points).payload
    theirs = _library_payload(monkeypatch, tmp_path / rel, points)

    assert set(ours) == set(theirs) - {"stable_id", "points"}, (
        "if the payload key set drifts from build_anlz_payload then the deck contract is broken"
    )
    for key in ("waveform", "beatgrid", "phrases", "vocals", "unreadable_anlz"):
        assert ours[key] == theirs[key], (
            f"if {key} drifts from build_anlz_payload then parity is broken"
        )
    assert ours["waveform"]["kind"] == kind
    assert len(ours["phrases"]) == 3 and ours["phrases"][1]["kind"] == 5, "the masked PSSI decoded"


def test_cue_rows_validate_as_the_library_hot_cue_model(tmp_path: Path) -> None:
    from apps.adapters.rekordbox.cues import HOT_CUE_SLOTS as LIBRARY_SLOTS
    from apps.webui.server.routes.rb_hot_cues import AnlzCueOut

    assert HOT_CUE_SLOTS == LIBRARY_SLOTS
    path = write_track(
        tmp_path,
        "PIONEER/USBANLZ/P003/00000001/ANLZ0000.DAT",
        dat=pmai(pqtz(grid(0, 128.0, 4))),
        ext=pmai(
            pco2(1, pcp2(1, 245, color=43), pcp2(5, 1531, comment="E", color=19)),
            pco2(0, pcp2(0, 100, loop_ms=4000)),
        ),
    )
    result = _read(tmp_path, path)

    cue_fields = set(AnlzCueOut.model_fields) - {"revision"}
    assert all(set(cue) == cue_fields for cue in result.payload["cues"]), (
        "if a stick cue's key set differs from the library AnlzCue then the deck contract is broken"
    )
    for cue in result.payload["cues"]:
        if cue["kind"] == "hot_cue":
            AnlzCueOut(**cue, revision="r")
    for hot in result.hot_cues:
        AnlzCueOut.model_validate(
            {
                "kind": "hot_cue",
                "slot": HOT_CUE_SLOTS[hot["slot"]],
                "in_ms": hot["position_ms"],
                "out_ms": None,
                "is_loop": False,
                "active_loop": False,
                "beat_loop_size": None,
                "color_table_index": hot["color"],
                "comment": hot["label"],
                "revision": "r",
            }
        )
    loops = [cue for cue in result.payload["cues"] if cue["kind"] == "loop"]
    assert loops == [
        {
            "kind": "loop",
            "slot": None,
            "in_ms": 100,
            "out_ms": 4000,
            "is_loop": True,
            "active_loop": False,
            "beat_loop_size": None,
            "color_table_index": None,
            "comment": None,
        }
    ]


# ----- live stick (read only) ------------------------------------------------------

STICK_ENV = "MDT_USB_STICK_ROOT"
STICK_BUDGET_MEDIAN_MS = 20.0
STICK_TIMING_PASSES = 3


def _stick_root() -> Path:
    raw = os.environ.get(STICK_ENV, "")
    if not raw:
        reason = f"{STICK_ENV} is unset: live stick ANLZ decode not run (set it to a mounted stick)"
        print(f"[SKIP] {reason}")
        pytest.skip(reason)
    root = Path(raw)
    if not (root / "PIONEER" / "rekordbox" / "export.pdb").is_file():
        pytest.fail(f"{STICK_ENV}={raw!r} is set but PIONEER/rekordbox/export.pdb is not there")
    return root


def test_live_stick_every_track_decodes_within_budget() -> None:
    from apps.analysis_waveform.native import waveform_materialization_backend
    from apps.sync.usb.pioneer.reader import read_usb_export
    from apps.webui.server.rb_vendor_pkg.own_beatgrid_overlay import _beatgrid_payload

    root = _stick_root()
    tracks = [t for t in read_usb_export(root)["tracks"] if t["analyze_path"]]
    assert tracks, "control: the stick export lists tracks with analysis"

    # Per-track best of STICK_TIMING_PASSES, then the median: the minimum is
    # the least load-sensitive estimate on a shared machine (timeit's advice).
    best_ms: dict[int, dict[int, float]] = {1200: {}, 38400: {}}
    results: dict[int, Any] = {}
    for points, best in best_ms.items():
        for _ in range(STICK_TIMING_PASSES if points == 1200 else 1):
            for track in tracks:
                start = time.perf_counter()
                results[track["id"]] = _read(root, track["analyze_path"], points=points)
                elapsed_ms = (time.perf_counter() - start) * 1000
                best[track["id"]] = min(best.get(track["id"], elapsed_ms), elapsed_ms)
    partial = {
        tid: r.payload["unreadable_anlz"]
        for tid, r in results.items()
        if r.payload["unreadable_anlz"]
    }
    medians = {points: statistics.median(best.values()) for points, best in best_ms.items()}
    print(
        f"[STICK] {len(tracks)} tracks: {len(tracks) - len(partial)} decode fully, "
        f"{len(partial)} with unreadable tags {partial}; median ms "
        f"{medians[1200]:.1f} at 1200 points (best of {STICK_TIMING_PASSES}), "
        f"{medians[38400]:.1f} at 38400 (one pass, waveform backend "
        f"{waveform_materialization_backend()}), max {max(best_ms[38400].values()):.1f}"
    )

    shared = {
        Path(t["analyze_path"]).stem: t
        for t in tracks
        if Path(t["analyze_path"]).parent.as_posix() == "/" + SHARED_DIR
    }
    assert set(shared) >= {"ANLZ0000", "ANLZ0001"}, (
        f"control: {SHARED_DIR} holds two tracks on this stick"
    )
    for stem, track in shared.items():
        expected, _ = _beatgrid_payload(
            {"PQTZ": AnlzFile.parse_file(root / track["analyze_path"].lstrip("/")).get_tag("PQTZ")}
        )
        assert results[track["id"]].payload["beatgrid"] == expected, (
            f"if {stem} is not decoded from its own .DAT then the shared-directory case is broken"
        )
    grids = [
        results[shared[stem]["id"]].payload["beatgrid"]["beats"]
        for stem in ("ANLZ0000", "ANLZ0001")
    ]
    assert grids[0] != grids[1], (
        "if ANLZ0001 gets ANLZ0000's grid then exact-file resolution is broken"
    )
    assert medians[1200] < STICK_BUDGET_MEDIAN_MS, (
        f"if the median stick decode exceeds {STICK_BUDGET_MEDIAN_MS} ms then deck load regresses"
    )
