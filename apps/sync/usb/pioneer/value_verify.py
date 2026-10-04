"""Read key, grid, and loudness values back from a rekordbox-exported USB stick.

Must not import ``writer_onelibrary`` or ``export_workflow`` (overlay-only writers).
"""
from __future__ import annotations

import enum
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from apps.shared.harmonic import key_to_camelot
from apps.sync.analysis_writeback import _FLOAT_TOL, _SCALAR_TABLE
from apps.sync.usb.pioneer.reader import (
    _resolve_pioneer_root,
    grid_summary_from_anlz,
    read_usb_export,
)
from apps.sync.usb.pioneer.value_verify_sidecar import (
    read_scalar_sidecar_from_onelibrary,
)

DENOMINATOR_LABEL = "tracks present on the stick, re-counted from export.pdb"


class FieldStatus(enum.Enum):
    PRESENT = "present"
    ABSENT = "absent"
    MATCH = "match"
    MISMATCH = "mismatch"
    UNREAD = "unread"


@dataclass(frozen=True, slots=True)
class FieldObservation:
    status: FieldStatus
    stick_value: str | None
    expected_value: str | None
    source: str
    note: str = ""


@dataclass(slots=True)
class FieldCounts:
    present: int = 0
    absent: int = 0
    match: int = 0
    mismatch: int = 0
    unread: int = 0

    def add(self, obs: FieldObservation) -> None:
        setattr(self, obs.status.value, getattr(self, obs.status.value) + 1)


@dataclass(frozen=True, slots=True)
class ExpectedGrid:
    beat_count: int
    first_bpm: float
    first_time_ms: int


@dataclass(frozen=True, slots=True)
class ExpectedTrack:
    filename: str | None = None
    title: str | None = None
    artist: str | None = None
    key: str | None = None
    loudness_lufs: float | None = None
    loudness_dbtp: float | None = None
    grid: ExpectedGrid | None = None


@dataclass(slots=True)
class TrackValues:
    track_id: int
    filename: str | None
    title: str | None
    artist: str | None
    key: FieldObservation
    grid: FieldObservation
    loudness_lufs: FieldObservation
    loudness_dbtp: FieldObservation


@dataclass(slots=True)
class StickValuesReport:
    pioneer_path: Path
    is_rekordbox_export: bool
    tracks_on_stick: int
    denominator_label: str
    key: FieldCounts
    loudness: FieldCounts
    grid: FieldCounts
    tracks: list[TrackValues]
    unread_reasons: list[str] = field(default_factory=list)
    not_on_stick: list[str] = field(default_factory=list)
    overlay_note: str = ""


def load_expected_json(path: Path) -> dict[str, ExpectedTrack]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, ExpectedTrack] = {}
    for row in raw.get("tracks", []):
        grid_raw = row.get("grid")
        grid = (
            ExpectedGrid(
                beat_count=int(grid_raw["beat_count"]),
                first_bpm=float(grid_raw["first_bpm"]),
                first_time_ms=int(grid_raw["first_time_ms"]),
            )
            if grid_raw is not None
            else None
        )
        exp = ExpectedTrack(
            filename=row.get("filename"),
            title=row.get("title"),
            artist=row.get("artist"),
            key=row.get("key"),
            loudness_lufs=row.get("loudness_lufs"),
            loudness_dbtp=row.get("loudness_dbtp"),
            grid=grid,
        )
        out[exp.filename or f"{exp.title}|{exp.artist}"] = exp
    return out


def _obs(
    status: FieldStatus,
    stick_value: str | None,
    expected_value: str | None,
    source: str,
    note: str = "",
) -> FieldObservation:
    return FieldObservation(status, stick_value, expected_value, source, note)


def _compare_key(
    stick_key: str | None, key_id: int, expected_key: str | None
) -> FieldObservation:
    if key_id == 0 or not stick_key:
        return _obs(
            FieldStatus.ABSENT,
            None,
            expected_key,
            "export.pdb",
        )
    if expected_key is None:
        return _obs(FieldStatus.PRESENT, stick_key, None, "export.pdb")
    try:
        same = str(key_to_camelot(stick_key)) == str(key_to_camelot(expected_key))
        return _obs(
            FieldStatus.MATCH if same else FieldStatus.MISMATCH,
            stick_key,
            expected_key,
            "export.pdb",
            note="" if same else "",
        )
    except ValueError:
        return _obs(
            FieldStatus.MISMATCH,
            stick_key,
            expected_key,
            "export.pdb",
            note="unparseable stick key",
        )


def _compare_grid(
    pioneer: Path, anlz_path: str | None, expected_grid: ExpectedGrid | None
) -> FieldObservation:
    expected_repr = (
        json.dumps(asdict(expected_grid), sort_keys=True) if expected_grid else None
    )
    if not anlz_path:
        return _obs(FieldStatus.ABSENT, None, expected_repr, "USBANLZ")
    device_path = PurePosixPath(anlz_path)
    if device_path.parts[:3] == ("/", "PIONEER", "USBANLZ"):
        # PDB paths are rooted at the USB volume, not the host filesystem.
        anlz_dir = pioneer.joinpath(*device_path.parts[2:])
        if not anlz_dir.resolve().is_relative_to(pioneer.resolve()):
            return _obs(
                FieldStatus.UNREAD, None, expected_repr, "USBANLZ",
                "device ANLZ path escapes the inspected Pioneer root",
            )
    else:
        local_path = Path(anlz_path)
        anlz_dir = local_path if local_path.is_absolute() else pioneer / local_path
    summary = grid_summary_from_anlz(anlz_dir)
    if summary is None:
        return _obs(FieldStatus.ABSENT, None, expected_repr, "USBANLZ")
    stick_value = (
        f"{summary['beat_count']}@{summary['first_bpm']:.1f}"
        f"#{summary['first_time_ms']}"
    )
    if expected_grid is None:
        return _obs(FieldStatus.PRESENT, stick_value, None, "USBANLZ")
    bpm_ok = abs(summary["first_bpm"] - expected_grid.first_bpm) <= 0.01
    matched = (
        summary["beat_count"] == expected_grid.beat_count
        and bpm_ok
        and summary["first_time_ms"] == expected_grid.first_time_ms
    )
    return _obs(
        FieldStatus.MATCH if matched else FieldStatus.MISMATCH,
        stick_value,
        expected_repr,
        "USBANLZ",
    )


def _compare_loudness_scalar(
    stick_value: str | None,
    expected_value: float | None,
    *,
    source: str,
    note: str = "",
) -> FieldObservation:
    expected_s = str(expected_value) if expected_value is not None else None
    if stick_value is None:
        return _obs(FieldStatus.ABSENT, None, expected_s, source, note=note)
    if expected_value is None:
        return _obs(FieldStatus.PRESENT, stick_value, None, source, note=note)
    try:
        same = abs(float(stick_value) - float(expected_value)) <= _FLOAT_TOL
        return _obs(
            FieldStatus.MATCH if same else FieldStatus.MISMATCH,
            stick_value,
            expected_s,
            source,
            note=note,
        )
    except ValueError:
        return _obs(
            FieldStatus.MISMATCH,
            stick_value,
            expected_s,
            source,
            note=note or "non-numeric loudness value",
        )


def _loudness_summary_status(
    lufs: FieldObservation, dbtp: FieldObservation
) -> FieldObservation:
    order = {
        FieldStatus.MISMATCH: 4,
        FieldStatus.ABSENT: 3,
        FieldStatus.UNREAD: 2,
        FieldStatus.MATCH: 1,
        FieldStatus.PRESENT: 0,
    }
    worse = lufs if order[lufs.status] >= order[dbtp.status] else dbtp
    return _obs(
        worse.status,
        lufs.stick_value or dbtp.stick_value,
        lufs.expected_value or dbtp.expected_value,
        lufs.source if lufs.source != "none" else dbtp.source,
        note="; ".join(filter(None, [lufs.note, dbtp.note])),
    )


def _find_expected_for_track(
    track: dict[str, Any], expected_list: list[ExpectedTrack]
) -> ExpectedTrack | None:
    filename = track.get("filename")
    title = track.get("title")
    artist = track.get("artist")
    for exp in expected_list:
        if exp.filename and filename and exp.filename == filename:
            return exp
        if (
            exp.title
            and exp.artist
            and title
            and artist
            and exp.title == title
            and exp.artist == artist
        ):
            return exp
    return None


def _not_on_stick_labels(
    stick_tracks: list[dict[str, Any]], expected: Mapping[str, ExpectedTrack] | None
) -> list[str]:
    if expected is None:
        return []
    missing: list[str] = []
    for exp in expected.values():
        label = exp.filename or f"{exp.title}|{exp.artist}"
        if not any(_find_expected_for_track(t, [exp]) for t in stick_tracks):
            missing.append(label)
    return missing


def verify_stick_values(
    pioneer_path: Path,
    *,
    expected: Mapping[str, ExpectedTrack] | None = None,
) -> StickValuesReport:
    pioneer_path = pioneer_path.resolve()
    if not pioneer_path.exists():
        raise FileNotFoundError(f"path not found: {pioneer_path}")

    pioneer = _resolve_pioneer_root(pioneer_path)
    pdb_path = pioneer / "rekordbox" / "export.pdb"
    one_lib_path = pioneer / "rekordbox" / "exportLibrary.db"

    if not pdb_path.exists():
        return StickValuesReport(
            pioneer_path=pioneer,
            is_rekordbox_export=False,
            tracks_on_stick=0,
            denominator_label=DENOMINATOR_LABEL,
            key=FieldCounts(),
            loudness=FieldCounts(),
            grid=FieldCounts(),
            tracks=[],
            overlay_note=(
                "export.pdb missing; overlay-only OneLibrary is not a rekordbox export"
            ),
        )

    stick_tracks = read_usb_export(pioneer)["tracks"]
    sidecar: dict[int, dict[str, str]] = {}
    contents_by_filename: dict[str, dict[str, Any]] = {}
    unread_reasons: list[str] = []
    if one_lib_path.exists():
        sidecar, contents_by_filename, unread_reasons = (
            read_scalar_sidecar_from_onelibrary(one_lib_path)
        )

    expected_list = list(expected.values()) if expected else []
    key_counts = FieldCounts()
    grid_counts = FieldCounts()
    loudness_counts = FieldCounts()
    track_rows: list[TrackValues] = []

    for track in stick_tracks:
        exp = _find_expected_for_track(track, expected_list)
        key_obs = _compare_key(
            track.get("key"), int(track.get("key_id", 0)), exp.key if exp else None
        )
        grid_obs = _compare_grid(
            pioneer, track.get("anlz_path"), exp.grid if exp else None
        )

        lufs_stick = dbtp_stick = None
        loudness_source = "none"
        loudness_note = f"{_SCALAR_TABLE} not present on stick"
        if unread_reasons and not contents_by_filename:
            loudness_source = "exportLibrary.db"
            loudness_note = unread_reasons[0]
        elif contents_by_filename:
            content = contents_by_filename.get(str(track.get("filename") or ""))
            if content is None:
                loudness_source = "exportLibrary.db"
                loudness_note = "could not join sidecar ContentID to PDB filename"
            else:
                fields = sidecar.get(int(content["id"]), {})
                lufs_stick = fields.get("loudness_lufs")
                dbtp_stick = fields.get("loudness_dbtp")
                loudness_source = "exportLibrary.db"
                loudness_note = ""

        if unread_reasons and not contents_by_filename:
            lufs_obs = _obs(
                FieldStatus.UNREAD,
                None,
                str(exp.loudness_lufs) if exp and exp.loudness_lufs is not None else None,
                loudness_source,
                loudness_note,
            )
            dbtp_obs = _obs(
                FieldStatus.UNREAD,
                None,
                str(exp.loudness_dbtp) if exp and exp.loudness_dbtp is not None else None,
                loudness_source,
                loudness_note,
            )
        else:
            lufs_obs = _compare_loudness_scalar(
                lufs_stick, exp.loudness_lufs if exp else None,
                source=loudness_source, note=loudness_note,
            )
            dbtp_obs = _compare_loudness_scalar(
                dbtp_stick, exp.loudness_dbtp if exp else None,
                source=loudness_source, note=loudness_note,
            )

        key_counts.add(key_obs)
        grid_counts.add(grid_obs)
        loudness_counts.add(_loudness_summary_status(lufs_obs, dbtp_obs))
        track_rows.append(
            TrackValues(
                track_id=int(track["id"]),
                filename=track.get("filename"),
                title=track.get("title"),
                artist=track.get("artist"),
                key=key_obs,
                grid=grid_obs,
                loudness_lufs=lufs_obs,
                loudness_dbtp=dbtp_obs,
            )
        )

    return StickValuesReport(
        pioneer_path=pioneer,
        is_rekordbox_export=True,
        tracks_on_stick=len(stick_tracks),
        denominator_label=DENOMINATOR_LABEL,
        key=key_counts,
        loudness=loudness_counts,
        grid=grid_counts,
        tracks=track_rows,
        unread_reasons=unread_reasons,
        not_on_stick=_not_on_stick_labels(stick_tracks, expected),
    )


def stick_values_to_jsonable(report: StickValuesReport) -> dict[str, Any]:
    def field(obs: FieldObservation) -> dict[str, Any]:
        return {
            "status": obs.status.value,
            "stick_value": obs.stick_value,
            "expected_value": obs.expected_value,
            "source": "stick" if obs.stick_value is not None else obs.source,
            "note": obs.note,
        }

    def counts(fc: FieldCounts) -> dict[str, int]:
        return {
            "present": fc.present,
            "absent": fc.absent,
            "match": fc.match,
            "mismatch": fc.mismatch,
            "unread": fc.unread,
        }

    return {
        "source": "stick",
        "pioneer_path": str(report.pioneer_path),
        "is_rekordbox_export": report.is_rekordbox_export,
        "tracks_on_stick": report.tracks_on_stick,
        "denominator_label": report.denominator_label,
        "overlay_note": report.overlay_note,
        "counts": {
            "key": counts(report.key),
            "loudness": counts(report.loudness),
            "grid": counts(report.grid),
        },
        "unread_reasons": report.unread_reasons,
        "not_on_stick": report.not_on_stick,
        "tracks": [
            {
                "track_id": t.track_id,
                "filename": t.filename,
                "title": t.title,
                "artist": t.artist,
                "key": field(t.key),
                "grid": field(t.grid),
                "loudness_lufs": field(t.loudness_lufs),
                "loudness_dbtp": field(t.loudness_dbtp),
            }
            for t in report.tracks
        ],
    }


def pioneer_export_exit_code(report: StickValuesReport, *, has_expected: bool) -> int:
    if not report.is_rekordbox_export:
        return 4
    if not has_expected:
        return 0
    for track in report.tracks:
        for obs in (track.key, track.grid, track.loudness_lufs, track.loudness_dbtp):
            if obs.expected_value is not None and obs.status in (
                FieldStatus.ABSENT,
                FieldStatus.MISMATCH,
            ):
                return 5
    return 0
