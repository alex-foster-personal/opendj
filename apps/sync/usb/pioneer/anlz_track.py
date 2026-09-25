"""One stick track's ANLZ analysis, in the library ``/anlz`` wire shape (USBPLAY-07).

``read_stick_track_analysis`` reads ONE track's ANLZ files straight off a
mounted rekordbox USB stick and returns the payload the deck already consumes
(``AnlzData`` in ``apps/webui/frontend/src/lib/rb/anlz-types.ts``) minus the
``stable_id`` and ``points`` keys the route stamps itself, plus the hot and
memory cues in a flat form for ``/hot-cues``.

Rules this module exists to keep:

* EXACT FILES, NEVER A DIRECTORY SCAN. ``analyze_path`` from ``export.pdb``
  names ``ANLZ000N.DAT``; ``.EXT`` and ``.2EX`` are that file with the suffix
  swapped. Two tracks can share one directory (``ANLZ0000.*`` and
  ``ANLZ0001.*``; P016/00024756 on the SSK test stick), so the library's
  ``anlz._first_tags`` (first tag of each type in the directory) would hand
  one track the other's grid.
* ONE BAD TAG COSTS ONE LANE. Each file is walked with a raw PMAI tag walker
  and every needed tag is decoded on its own, with pyrekordbox's per-tag
  structs where they hold. A whole-file pyrekordbox parse fails on 8 of 563
  ``.EXT`` files on the SSK stick (44-byte PCP2 entries, PQT2/PCPT const
  checks) and would drop every cue. Each failure is recorded in
  ``unreadable_anlz`` as ``<file>:<fourcc>: <error>`` and logged.
* READ ONLY AND CONTAINED. Every file must resolve under
  ``<volume>/PIONEER/USBANLZ``. Nothing here writes.

Sources, in order:

* beatgrid: PQTZ (.DAT), stamped ``source: rekordbox``.
* cues: PCO2 (.EXT) per list (hot, memory); a list with no decodable PCO2
  falls back to the PCOB lists of .DAT, then .EXT (rekordbox splits the
  legacy PCOB copies across both files).
* waveform: PWV6 + PWV7 (.2EX) tri-band; else mono, preview PWAV (.DAT),
  detail PWV5 then PWV3 (.EXT). Band shaping is the library's own
  (``apps.analysis_waveform.bands``).
* phrases: PSSI (.EXT), XOR-unmasked exactly as pyrekordbox's file parser does.
* vocals: always ``{"status": "not_analyzed"}``. A PVDI tag, when present, is
  listed in ``unreadable_anlz`` instead of decoded: the PVDI decoder lives in
  ``apps.webui``, which this package may not import.

``.importlinter`` (webui-is-the-top-layer) forbids ``apps.sync`` importing
``apps.webui``, so the beatgrid and phrase shaping that ``build_anlz_payload``
takes from webui helpers is restated here and pinned to those helpers by a
parity test (``tests/sync/usb/test_stick_anlz.py``).

-Claude
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, TypedDict, TypeVar

import numpy as np
from pyrekordbox.anlz.file import XOR_MASK
from pyrekordbox.anlz.tags import TAGS

from apps.analysis_waveform.bands import _bands_payload, _mono_bands, _tri_bands

log = logging.getLogger(__name__)

T = TypeVar("T")

ANLZ_SUFFIXES: tuple[str, ...] = (".DAT", ".EXT", ".2EX")
# apps.adapters.rekordbox.cues.HOT_CUE_SLOTS, restated: importing that module
# pulls in fastapi (0.7 s measured). Pinned equal by a test.
HOT_CUE_SLOTS: str = "ABCDEFGH"
BEATGRID_SOURCE: str = "rekordbox"
_HOT_LIST: int = 1
_MEMORY_LIST: int = 0
_NO_LOOP: frozenset[int] = frozenset({0, 0xFFFFFFFF})
_PCP2_MIN_LEN: int = 44  # entry with no comment and no color bytes
_TAG_HEADER = struct.Struct(">4sII")
_EMPTY_BANDS: dict[str, Any] = {"length": 0, "low": [], "mid": [], "high": []}


# ----- public contract ------------------------------------------------------


class StickAnlzError(Exception):
    """A typed refusal; ``code`` is the ``detail.code`` the route answers with."""

    code: ClassVar[str]


class StickAnlzPathRefused(StickAnlzError):
    """``analyze_path`` does not name a .DAT/.EXT/.2EX under PIONEER/USBANLZ."""

    code = "USB_PATH_OUTSIDE_VOLUME"


class StickAnlzFileMissing(StickAnlzError):
    """The track has no ``analyze_path``, or its .DAT is not on the stick."""

    code = "USB_FILE_MISSING"


class StickAnlzUnreadable(StickAnlzError):
    """No ANLZ file of the track yielded a single tag (library: ANALYSIS_NOT_FOUND)."""

    code = "ANALYSIS_NOT_FOUND"


class StickHotCue(TypedDict):
    slot: int  # 0..7 = A..H
    position_ms: int
    color: int | None  # rekordbox color table index, as AnlzCueOut.color_table_index
    label: str | None  # the cue comment


class StickMemoryCue(TypedDict):
    position_ms: int
    color: int | None
    label: str | None


@dataclass(frozen=True)
class StickTrackAnalysis:
    payload: dict[str, Any]
    hot_cues: list[StickHotCue]
    memory_cues: list[StickMemoryCue]


def read_stick_track_analysis(
    *, volume_root: Path, analyze_path: str, points: int
) -> StickTrackAnalysis:
    """Decode one stick track's grid, cues, waveform and phrases.

    Raises :class:`StickAnlzPathRefused`, :class:`StickAnlzFileMissing` or
    :class:`StickAnlzUnreadable`; an ``OSError`` reading the .DAT (stick
    pulled mid-read) propagates unchanged.
    """
    if points < 1:
        raise ValueError(f"points must be >= 1, got {points}")
    tags = _TrackTags.read(_resolve_anlz_files(volume_root, analyze_path))
    beatgrid, times = _beatgrid(tags)
    cues = _select_cues(tags)
    payload: dict[str, Any] = {
        "waveform": _waveform(tags, points),
        "beatgrid": beatgrid,
        "phrases": _phrases(tags, times),
        "vocals": {"status": "not_analyzed"},
        "cues": [_cue_payload(cue) for cue in sorted(cues, key=_cue_order)],
    }
    tags.note_undecoded_vocals()
    payload["unreadable_anlz"] = list(tags.unreadable)
    hot = sorted((cue for cue in cues if cue.hot_cue), key=lambda cue: cue.hot_cue)
    memory = sorted((cue for cue in cues if not cue.hot_cue), key=_cue_order)
    return StickTrackAnalysis(
        payload=payload,
        hot_cues=[
            StickHotCue(
                slot=cue.hot_cue - 1,
                position_ms=cue.in_ms,
                color=cue.color,
                label=cue.comment,
            )
            for cue in hot
        ],
        memory_cues=[
            StickMemoryCue(position_ms=cue.in_ms, color=cue.color, label=cue.comment)
            for cue in memory
        ],
    )


# ----- files + raw PMAI walk ------------------------------------------------


@dataclass(frozen=True)
class _RawTag:
    file_name: str
    fourcc: str
    head_len: int
    data: bytes  # the whole tag, its 12-byte generic header included


def _resolve_anlz_files(volume_root: Path, analyze_path: str) -> dict[str, Path]:
    """The track's own .DAT/.EXT/.2EX, each contained in PIONEER/USBANLZ."""
    if not analyze_path:
        raise StickAnlzFileMissing("track has no analyze_path in export.pdb")
    volume = volume_root.resolve()
    anlz_root = (volume / "PIONEER" / "USBANLZ").resolve()
    if not anlz_root.is_relative_to(volume):
        raise StickAnlzPathRefused(f"{anlz_root} escapes the volume {volume}")
    dat = volume / analyze_path.lstrip("/")
    if dat.suffix.upper() != ".DAT":
        raise StickAnlzPathRefused(f"analyze_path {analyze_path!r} does not name a .DAT")
    files: dict[str, Path] = {}
    for suffix in ANLZ_SUFFIXES:
        candidate = (dat if suffix == ".DAT" else dat.with_suffix(suffix)).resolve()
        if not candidate.is_relative_to(anlz_root):
            raise StickAnlzPathRefused(
                f"{analyze_path!r} ({suffix}) resolves outside {anlz_root}: {candidate}"
            )
        files[suffix] = candidate
    return files


def _walk_pmai(file_name: str, buf: bytes, unreadable: list[str]) -> list[_RawTag]:
    """Tag headers of one PMAI container; a broken length ends the walk, recorded."""
    if len(buf) < 12 or buf[:4] != b"PMAI":
        unreadable.append(f"{file_name}:PMAI: not an ANLZ PMAI container")
        return []
    head_len, file_len = struct.unpack_from(">II", buf, 4)
    if not 12 <= head_len <= len(buf):
        unreadable.append(f"{file_name}:PMAI: header length {head_len} does not fit")
        return []
    end = min(file_len, len(buf))
    tags: list[_RawTag] = []
    offset = head_len
    while offset < end:
        if offset + _TAG_HEADER.size > len(buf):
            unreadable.append(f"{file_name}:PMAI: truncated tag header at {offset}")
            break
        fourcc_raw, tag_head, tag_len = _TAG_HEADER.unpack_from(buf, offset)
        fourcc = fourcc_raw.decode("latin-1")
        if not 12 <= tag_head <= tag_len or offset + tag_len > len(buf):
            unreadable.append(
                f"{file_name}:{fourcc}: tag length {tag_len} (header {tag_head}) "
                f"at offset {offset} does not fit the {len(buf)}-byte file"
            )
            break
        tags.append(_RawTag(file_name, fourcc, tag_head, buf[offset : offset + tag_len]))
        offset += tag_len
    return tags


class _TrackTags:
    """Raw tags of one track, keyed (suffix, fourcc), plus the failure record."""

    def __init__(self, raw: dict[tuple[str, str], list[_RawTag]], unreadable: list[str]):
        self.raw = raw
        self.unreadable = unreadable

    @classmethod
    def read(cls, files: dict[str, Path]) -> _TrackTags:
        raw: dict[tuple[str, str], list[_RawTag]] = {}
        unreadable: list[str] = []
        for suffix, path in files.items():
            try:
                buf = path.read_bytes()
            except FileNotFoundError as exc:
                if suffix == ".DAT":
                    raise StickAnlzFileMissing(f"{path} is not on the stick") from exc
                continue  # absent sibling is real: pre-rekordbox-6 exports have no .2EX
            except OSError as exc:
                if suffix == ".DAT":
                    raise
                unreadable.append(f"{path.name}:FILE: {type(exc).__name__}: {exc}")
                log.warning("stick ANLZ %s unreadable: %s", path, exc)
                continue
            for tag in _walk_pmai(path.name, buf, unreadable):
                raw.setdefault((suffix, tag.fourcc), []).append(tag)
        if not raw:
            raise StickAnlzUnreadable(
                f"no ANLZ tag could be read for {files['.DAT']}: {unreadable}"
            )
        return cls(raw, unreadable)

    def decode_all(self, suffix: str, fourcc: str, decode: Callable[[_RawTag], T]) -> list[T]:
        decoded: list[T] = []
        for tag in self.raw.get((suffix, fourcc), ()):
            result = self._decode(tag, decode)
            if result is not None:
                decoded.append(result)
        return decoded

    def decode_first(self, suffix: str, fourcc: str, decode: Callable[[_RawTag], T]) -> T | None:
        for tag in self.raw.get((suffix, fourcc), ()):
            result = self._decode(tag, decode)
            if result is not None:
                return result
        return None

    def note_undecoded_vocals(self) -> None:
        for tag in self.raw.get((".2EX", "PVDI"), ()):
            self.unreadable.append(
                f"{tag.file_name}:PVDI: vocal regions are not decoded for stick "
                "tracks; vocals report not_analyzed"
            )

    def _decode(self, tag: _RawTag, decode: Callable[[_RawTag], T]) -> T | None:
        try:
            return decode(tag)
        except Exception as exc:  # noqa: BLE001 -- one bad tag costs one lane; recorded below
            self.unreadable.append(f"{tag.file_name}:{tag.fourcc}: {type(exc).__name__}: {exc}")
            log.warning("stick ANLZ tag %s:%s unreadable: %s", tag.file_name, tag.fourcc, exc)
            return None


# ----- beatgrid + phrases (pinned to the library helpers by a parity test) ----


def _beatgrid(tags: _TrackTags) -> tuple[dict[str, Any], list[float]]:
    """Same shape as own_beatgrid_overlay._beatgrid_payload, from the .DAT PQTZ."""
    pqtz = tags.decode_first(".DAT", "PQTZ", lambda tag: TAGS["PQTZ"](tag.data))
    if pqtz is None:
        return {"source": BEATGRID_SOURCE, "beat_count": 0, "beats": []}, []
    beats = pqtz.get_beats()
    bpms = pqtz.get_bpms()
    times = [float(t) for t in pqtz.get_times()]
    return {
        "source": BEATGRID_SOURCE,
        "beat_count": len(beats),
        "beats": [
            {"n": int(n), "bpm": round(float(bpm), 2), "t": round(t, 3)}
            for n, bpm, t in zip(beats, bpms, times, strict=True)
        ],
    }, times


def _unmask_pssi(data: bytes) -> bytes:
    """Undo the XOR mask rekordbox applies to exported PSSI (pyrekordbox AnlzFile._parse)."""
    mood = int.from_bytes(data[18:20], "big")
    if 1 <= mood <= 3:
        return data
    len_entries = int.from_bytes(data[16:18], "big")
    unmasked = bytearray(data)
    for index in range(len(unmasked) - 18):
        unmasked[18 + index] ^= (XOR_MASK[index % len(XOR_MASK)] + len_entries) % 256
    return bytes(unmasked)


def _phrases(tags: _TrackTags, times: list[float]) -> list[dict[str, Any]]:
    """Same shape as anlz._phrases_payload, from the .EXT PSSI."""
    if not times:
        return []

    def phrases_of(tag: _RawTag) -> list[dict[str, Any]]:
        pssi = TAGS["PSSI"](_unmask_pssi(tag.data)).content

        def time_of_beat(beat: int) -> float:
            return round(times[min(max(beat - 1, 0), len(times) - 1)], 3)

        mood = int(pssi.mood)
        entries = list(pssi.entries)
        return [
            {
                "start_s": time_of_beat(int(entry.beat)),
                "end_s": time_of_beat(
                    int(entries[i + 1].beat) if i + 1 < len(entries) else int(pssi.end_beat)
                ),
                "kind": int(entry.kind),
                "mood": mood,
            }
            for i, entry in enumerate(entries)
        ]

    return tags.decode_first(".EXT", "PSSI", phrases_of) or []


# ----- waveform ---------------------------------------------------------------


@dataclass(frozen=True)
class _Heights:
    """Adapter so raw 0..31 heights ride ``_mono_bands`` (it reads ``.get()[0]``)."""

    heights: np.ndarray

    def get(self) -> tuple[np.ndarray]:
        return (self.heights,)


def _pwv5_mono_bands(tag: _RawTag) -> dict[str, np.ndarray]:
    """PWV5 color detail -> its 5-bit height lane (bits 2..6 of each u16)."""
    entry_bytes, entries = struct.unpack_from(">II", tag.data, 12)
    if entry_bytes != 2:
        raise ValueError(f"PWV5 entry size {entry_bytes} != 2")
    body = tag.data[tag.head_len : tag.head_len + 2 * entries]
    if len(body) != 2 * entries:
        raise ValueError(f"PWV5 holds {len(body)} bytes for {entries} entries")
    raw = np.frombuffer(body, dtype=">u2")
    return _mono_bands(_Heights((raw & 0x7C) >> 2))


def _waveform(tags: _TrackTags, points: int) -> dict[str, Any]:
    preview = tags.decode_first(".2EX", "PWV6", lambda tag: _tri_bands(TAGS["PWV6"](tag.data)))
    detail = tags.decode_first(".2EX", "PWV7", lambda tag: _tri_bands(TAGS["PWV7"](tag.data)))
    kind = "tri"
    if preview is None or detail is None:
        kind = "mono"
        preview = tags.decode_first(".DAT", "PWAV", lambda tag: _mono_bands(TAGS["PWAV"](tag.data)))
        detail = tags.decode_first(".EXT", "PWV5", _pwv5_mono_bands)
        if detail is None:
            detail = tags.decode_first(
                ".EXT", "PWV3", lambda tag: _mono_bands(TAGS["PWV3"](tag.data))
            )
    return {
        "kind": kind,
        "preview": _bands_payload(preview, points) if preview else dict(_EMPTY_BANDS),
        "detail": _bands_payload(detail, points) if detail else dict(_EMPTY_BANDS),
    }


# ----- cues -------------------------------------------------------------------


@dataclass(frozen=True)
class _Cue:
    hot_cue: int  # 0 = memory, 1..8 = A..H
    in_ms: int
    out_ms: int | None
    color: int | None
    comment: str | None


def _loop_end(loop_ms: int) -> int | None:
    return None if loop_ms in _NO_LOOP else loop_ms


def _checked_hot_cue(hot_cue: int) -> int:
    if not 0 <= hot_cue <= len(HOT_CUE_SLOTS):
        raise ValueError(f"hot cue index {hot_cue} is outside 0..{len(HOT_CUE_SLOTS)}")
    return hot_cue


def _decode_pco2(tag: _RawTag) -> tuple[int, list[_Cue]]:
    """Raw PCO2 decode. pyrekordbox's PCP2 struct demands color bytes that a
    44-byte entry does not carry, so it cannot read those entries at all."""
    data = tag.data
    list_type = int.from_bytes(data[12:16], "big")
    if list_type not in (_HOT_LIST, _MEMORY_LIST):
        raise ValueError(f"PCO2 list type {list_type} is neither hot (1) nor memory (0)")
    count = int.from_bytes(data[16:18], "big")
    cues: list[_Cue] = []
    offset = tag.head_len
    for index in range(count):
        if offset + _PCP2_MIN_LEN > len(data) or data[offset : offset + 4] != b"PCP2":
            raise ValueError(f"PCP2 entry {index} of {count} missing at offset {offset}")
        (entry_len,) = struct.unpack_from(">I", data, offset + 8)
        (comment_len,) = struct.unpack_from(">I", data, offset + 40)
        if entry_len < _PCP2_MIN_LEN + comment_len or offset + entry_len > len(data):
            raise ValueError(f"PCP2 entry {index} length {entry_len} does not fit")
        hot_cue, time_ms, loop_ms = struct.unpack_from(">I4xII", data, offset + 12)
        comment_end = offset + _PCP2_MIN_LEN + comment_len
        comment = data[offset + _PCP2_MIN_LEN : comment_end].decode("utf-16-be")
        has_color = comment_end + 4 <= offset + entry_len
        cues.append(
            _Cue(
                hot_cue=_checked_hot_cue(hot_cue),
                in_ms=time_ms,
                out_ms=_loop_end(loop_ms),
                color=data[comment_end] if has_color else None,
                comment=comment.rstrip("\x00") or None,
            )
        )
        offset += entry_len
    slots = [cue.hot_cue for cue in cues if cue.hot_cue]
    if len(slots) != len(set(slots)):
        raise ValueError(f"PCO2 names a hot cue slot twice: {sorted(slots)}")
    return list_type, cues


def _decode_pcob(tag: _RawTag) -> tuple[str, int, list[_Cue]]:
    content = TAGS["PCOB"](tag.data).content
    list_names = {"hotcue": _HOT_LIST, "memory": _MEMORY_LIST}
    if str(content.cue_type) not in list_names:
        raise ValueError(f"PCOB list type {content.cue_type!r} is neither hotcue nor memory")
    return (
        tag.file_name,
        list_names[str(content.cue_type)],
        [
            _Cue(
                hot_cue=_checked_hot_cue(int(entry.hot_cue)),
                in_ms=int(entry.time),
                out_ms=_loop_end(int(entry.loop_time)),
                color=None,
                comment=None,
            )
            for entry in content.entries
        ],
    )


def _select_cues(tags: _TrackTags) -> list[_Cue]:
    """PCO2 per list; a list with no decodable PCO2 takes the PCOB copies."""
    pco2: dict[int, list[_Cue]] = {}
    for list_type, cues in tags.decode_all(".EXT", "PCO2", _decode_pco2):
        pco2.setdefault(list_type, []).extend(cues)
    missing = [lt for lt in (_HOT_LIST, _MEMORY_LIST) if lt not in pco2]
    if not missing:
        return [*pco2[_HOT_LIST], *pco2[_MEMORY_LIST]]
    pcob = {suffix: tags.decode_all(suffix, "PCOB", _decode_pcob) for suffix in (".DAT", ".EXT")}
    chosen = [cue for cues in pco2.values() for cue in cues]
    for list_type in missing:
        for suffix in (".DAT", ".EXT"):
            for file_name, pcob_type, cues in pcob[suffix]:
                if pcob_type == list_type:
                    chosen.extend(_merge_pcob(chosen, cues, file_name, tags.unreadable))
    return chosen


def _merge_pcob(
    kept: list[_Cue], candidates: list[_Cue], file_name: str, unreadable: list[str]
) -> list[_Cue]:
    """PCOB cues not already kept; a hot slot claimed twice keeps the first (.DAT)."""
    added: list[_Cue] = []
    for cue in candidates:
        if cue in kept or cue in added:
            continue
        if cue.hot_cue and any(other.hot_cue == cue.hot_cue for other in (*kept, *added)):
            unreadable.append(
                f"{file_name}:PCOB: hot cue {HOT_CUE_SLOTS[cue.hot_cue - 1]} at "
                f"{cue.in_ms} ms conflicts with an earlier copy; kept the earlier one"
            )
            continue
        added.append(cue)
    return added


def _cue_order(cue: _Cue) -> tuple[int, int]:
    return cue.in_ms, cue.hot_cue


def _cue_payload(cue: _Cue) -> dict[str, Any]:
    """One cue in the library AnlzCue shape (rb_vendor_pkg.db.fetch_cues)."""
    is_loop = cue.out_ms is not None
    if cue.hot_cue:
        kind, slot = "hot_cue", HOT_CUE_SLOTS[cue.hot_cue - 1]
    elif is_loop:
        kind, slot = "loop", None
    else:
        kind, slot = "memory", None
    return {
        "kind": kind,
        "slot": slot,
        "in_ms": cue.in_ms,
        "out_ms": cue.out_ms,
        "is_loop": is_loop,
        # ANLZ carries no active-loop flag and no djmdCue BeatLoopSize.
        "active_loop": False,
        "beat_loop_size": None,
        "color_table_index": cue.color,
        "comment": cue.comment,
    }


__all__ = [
    "ANLZ_SUFFIXES",
    "BEATGRID_SOURCE",
    "HOT_CUE_SLOTS",
    "StickAnlzError",
    "StickAnlzFileMissing",
    "StickAnlzPathRefused",
    "StickAnlzUnreadable",
    "StickHotCue",
    "StickMemoryCue",
    "StickTrackAnalysis",
    "read_stick_track_analysis",
]
