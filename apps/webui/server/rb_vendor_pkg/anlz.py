"""ANLZ decode: PMAI waveform/preview, PVDI vocals, pyrekordbox tag decode, orchestrator.

Split out of ``apps/webui/server/rb_vendor.py`` per the T3b decomposition
(``.planning/t3b-decomposition-map.md`` section 2, slice S1). Covers clusters
C2 (raw PMAI waveform decode), C3 (PVDI vocal intensity + demucs merge), C6
(ANLZ tag decode to payload), and C9 (the ``build_anlz_payload`` orchestrator).
Functions are moved verbatim; no behavior change.

Path resolution (C1), the error constructors (C1) and the config constants
(C0) used to live in ``rb_vendor.py``, which also re-exported this module --
so the two depended on each other's names and every reference here had to be
a function-local import to dodge the deadlock. T3b wave 4 moved those
clusters to ``apps/adapters/rekordbox/`` (config, errors, models, paths),
which this module can import freely: the dependency runs one way now.

The local-import form is kept for those references. They are cold-path
lookups on already-loaded modules, and hoisting them would be a behavioural
change for no gain -- but each one names the module that owns the symbol
instead of the facade, so the cycle is gone rather than merely deferred.

``VOCAL_CACHE_DIR`` is read off the ``config`` module at call time rather than
imported by value: it is a rebindable override the test suite assigns to. See
``apps/adapters/rekordbox/config.py``.
"""

from __future__ import annotations

import base64
import logging
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

from apps.analysis_waveform.bands import (
    STRIP_COLUMNS,
    _bands_payload,
    _mono_bands,
    _tri_bands,
)
from apps.vocals import cache as vocal_cache
from apps.webui.server.rb_vendor_pkg.own_beatgrid_overlay import _beatgrid_payload
from apps.webui.server.rb_vendor_pkg.own_overlays import apply_own_overlays
from apps.webui.server.rb_vendor_pkg.row_hydration_cache import (
    _PREVIEW_CACHE,
    _VOCALS_CACHE,
)

if TYPE_CHECKING:
    from apps.adapters.rekordbox.models import RbContent
    from apps.shared.platform_paths import AssetResolver

log = logging.getLogger(__name__)


class BytesSource(Protocol):
    """A ``Path``, or bytes already read through a containment walk (row_assets)."""

    def read_bytes(self) -> bytes: ...

# --- preview strip (SPIKE-A1 / SPIKE-SUMMARY section 2) ---
# One width for every preview source, owned by apps.analysis_waveform.bands so
# the ANLZ strip and the locally decoded strip cannot drift apart.
PREVIEW_COLUMNS: int = STRIP_COLUMNS  # 1200 -> 120 peak-max downsample (10:1)
_PWV4_LUMINANCE_BYTE: int = 0  # mono height byte, 0..127
_PWAV_HEIGHT_MASK: int = 0x1F  # low 5 bits = height 0..31 (A1 tag table)

# --- vocals (SPIKE-B1 / SPIKE-B2 calibrated params) ---
VOCAL_INTENSITY_MIN: int = 1  # PVDI frame value (0..4) counted as vocal
VOCAL_MERGE_GAP_S: float = 1.5  # merge regions separated by < this gap
VOCAL_MIN_REGION_S: float = 1.0  # drop merged regions shorter than this
# PVDI fixed header bytes at section offset 12..20: u16 reserved=0x0000,
# u16 hop=1024, u16 rate=22050, u16 version=1. Reject deviations
# from this supported format; private corpus counts are not included.
_PVDI_FIXED_HEADER: bytes = bytes.fromhex("0000040056220001")
_PVDI_HOP: int = 1024
_PVDI_RATE: int = 22050


# ----- raw PMAI section walk (preview strips + vocals) ------------------------
# Ported from the spike PoCs (credit where the byte-exact logic was proven):
#   * PWV6 walker:  .tmp/.tmp_spike_a1_bench_dump.py::extract_pwv6_raw (SPIKE-A1)
#   * PVDI walker + regions: .tmp/.tmp_spike_b1_pvdi_regions.py (SPIKE-B1)
# pyrekordbox 0.4.4 cannot serve either tag (PWV6 tag.get() raises KeyError: 0,
# PVDI is dropped with a "not supported" warning). Private timing
# measurements are not included in the public source.


def _iter_pmai_sections(buf: bytes) -> Iterator[tuple[bytes, int, int, int]]:
    """Yield ``(fourcc, offset, head_len, total_len)`` over a PMAI container."""
    if buf[:4] != b"PMAI":
        raise ValueError("not an ANLZ PMAI container")
    off = struct.unpack(">I", buf[4:8])[0]
    while off + 12 <= len(buf):
        fourcc = buf[off : off + 4]
        head_len, total_len = struct.unpack(">II", buf[off + 4 : off + 12])
        if total_len <= 0:
            raise ValueError(f"corrupt ANLZ section length at offset {off}")
        yield fourcc, off, head_len, total_len
        off += total_len


def _read_pwv6_tri(path: BytesSource) -> np.ndarray | None:
    """.2EX PWV6 -> ``(n, 3)`` uint8 columns [low, mid, hi], or None if absent."""
    buf = path.read_bytes()
    for fourcc, off, head_len, _total_len in _iter_pmai_sections(buf):
        if fourcc != b"PWV6":
            continue
        entry_bytes, entries = struct.unpack(">II", buf[off + 12 : off + 20])
        if entry_bytes != 3:
            raise ValueError(f"PWV6 entry size {entry_bytes} != 3 in {path}")
        return np.frombuffer(buf, np.uint8, entries * 3, off + head_len).reshape(
            entries, 3
        )
    return None


def _read_pwv4_mono(path: BytesSource) -> np.ndarray | None:
    """.EXT PWV4 luminance byte (0..127) duplicated to 3 bands, or None.

    PWV4 is an RGB *colour* preview (6 bytes/col); its r/g/b bytes are display
    colours, not frequency bands, so mapping them onto low/mid/hi would invent
    data. Byte 0 supplies a mono luminance/height envelope -- serve that,
    duplicated, exactly like the PWAV mono fallback.
    """
    buf = path.read_bytes()
    for fourcc, off, head_len, _total_len in _iter_pmai_sections(buf):
        if fourcc != b"PWV4":
            continue
        entry_bytes, entries = struct.unpack(">II", buf[off + 12 : off + 20])
        if entry_bytes != 6:
            raise ValueError(f"PWV4 entry size {entry_bytes} != 6 in {path}")
        cols = np.frombuffer(buf, np.uint8, entries * 6, off + head_len).reshape(
            entries, 6
        )
        lum = cols[:, _PWV4_LUMINANCE_BYTE] & 0x7F
        return np.repeat(lum[:, np.newaxis], 3, axis=1)
    return None


def _read_pwav_mono(path: BytesSource) -> np.ndarray | None:
    """.DAT PWAV heights (low 5 bits, 0..31) duplicated to 3 bands, or None."""
    buf = path.read_bytes()
    for fourcc, off, head_len, total_len in _iter_pmai_sections(buf):
        if fourcc != b"PWAV":
            continue
        entries = struct.unpack(">I", buf[off + 12 : off + 16])[0]
        if entries != total_len - head_len:
            raise ValueError(
                f"PWAV length mismatch in {path}: "
                f"{entries} entries vs {total_len - head_len} payload bytes"
            )
        heights = (
            np.frombuffer(buf, np.uint8, entries, off + head_len) & _PWAV_HEIGHT_MASK
        )
        return np.repeat(heights[:, np.newaxis], 3, axis=1)
    return None


def _peak_downsample_cols(cols: np.ndarray, width: int) -> np.ndarray:
    """Peak-max downsample ``(n, 3)`` columns to ``(width, 3)`` (A1: max per
    bucket, not mean, so transients survive)."""
    n = cols.shape[0]
    if n < width:
        raise ValueError(
            f"cannot downsample {n} ANLZ columns to {width}: all known preview "
            f"tags carry >= 400 columns, so this is corrupt data"
        )
    edges = (np.arange(width) * n) // width
    return np.maximum.reduceat(cols, edges, axis=0)


# Fallback chain per SPIKE-A1 section 4 / SPIKE-SUMMARY: PWV6 (.2EX tri-band)
# -> PWV4 (.EXT, mono luminance) -> PWAV (.DAT, mono blue) -> (None, None).
_PREVIEW_SOURCES: tuple[tuple[str, Any], ...] = (
    (".2EX", _read_pwv6_tri),
    (".EXT", _read_pwv4_mono),
    (".DAT", _read_pwav_mono),
)


def encode_preview_strip(cols: np.ndarray) -> tuple[str, int]:
    """``(preview_b64, preview_max)`` for one decoded preview tag."""
    strip = _peak_downsample_cols(cols, PREVIEW_COLUMNS)
    return base64.b64encode(strip.tobytes()).decode("ascii"), int(strip.max())


def preview_strip(
    analysis_data_path: str | None,
    *,
    resolver: AssetResolver | None = None,
) -> tuple[str | None, int | None]:
    """Return ``(preview_b64, preview_max)`` for one track, or ``(None, None)``.

    ``preview_b64`` encodes ``uint8[120][3]`` interleaved [low, mid, hi] per
    column, peak-downsampled from the ANLZ preview tag (contract item 1).
    ``preview_max`` is the per-track max band value -- clients normalise by it,
    never by a fixed assumed maximum.

    ``(None, None)`` is the real "no ANLZ analysis" state (no path, files
    missing, or no preview tag anywhere in the chain) -- never zeros.
    Results are cached per AnalysisDataPath and revalidated by source mtime.

    ``resolver`` (pin ad59ac), when passed by a bulk caller such as
    ``build_track_rows``, memoises the containment resolution below for the
    lifetime of that one caller's own call -- see
    :class:`apps.shared.platform_paths.AssetResolver`. Omitted here (the
    default), every call re-walks the filesystem, unchanged.

    Pin 66b9602c5887: a per-suffix containment failure (e.g. a transient
    ``OSError`` from one ``Path.resolve()`` call) used to abort the WHOLE
    PWV6 -> PWV4 -> PWAV chain on the first candidate, silently producing
    "missing preview" for a track with a perfectly good fallback source
    sitting right next to the one that failed. Each suffix's failure now
    only rules out THAT suffix, like the pre-existing ``is_file()``
    check below -- the chain is exhausted, and only then logged, once
    every suffix has been tried.
    """
    from apps.adapters.rekordbox.paths import _asset_sibling, resolve_asset_path

    if not analysis_data_path:
        return None, None
    mapped = resolve_asset_path(analysis_data_path, resolver=resolver)
    if mapped.resolved is None:
        return None, None  # unmapped on this platform: a real "no analysis" state
    dat = mapped.resolved
    for suffix, reader in _PREVIEW_SOURCES:
        derived = _asset_sibling(mapped, dat.with_suffix(suffix), resolver=resolver)
        if derived.resolved is None:
            log.warning(
                "preview_strip: %s sibling containment failed (%s) for %s -- trying next source",
                suffix, derived.reason, analysis_data_path,
            )
            continue
        source = derived.resolved
        if not source.is_file():
            continue
        mtime = source.stat().st_mtime
        hit = _PREVIEW_CACHE.get(analysis_data_path, str(source), mtime)
        if hit is not None:
            return hit
        cols = reader(source)
        if cols is None:
            continue
        return _PREVIEW_CACHE.put(
            analysis_data_path, str(source), mtime, encode_preview_strip(cols)
        )
    log.warning("preview_strip: no preview in PWV6/PWV4/PWAV for %s -- (None, None)", analysis_data_path)
    return None, None


# ----- vocals (PVDI -- SPIKE-B1 decode, SPIKE-B2 calibrated params) -----------


def read_pvdi(path_2ex: BytesSource) -> tuple[float, bytes] | None:
    """Return ``(fps, envelope)`` from a .2EX, or None when PVDI is absent.

    Absence is a real state, including tracks analyzed before rekordbox 7.
    Callers must surface it as "not analyzed", never as an empty bar.
    Malformed PVDI raises: deviations from the supported fixed header
    must fail loudly rather than decode garbage.
    """
    buf = path_2ex.read_bytes()
    for fourcc, off, head_len, _total_len in _iter_pmai_sections(buf):
        if fourcc != b"PVDI":
            continue
        fixed = buf[off + 12 : off + 20]
        if fixed != _PVDI_FIXED_HEADER:
            raise ValueError(
                f"PVDI fixed header changed in {path_2ex}: "
                f"{fixed.hex()} != {_PVDI_FIXED_HEADER.hex()} -- format bump?"
            )
        count = struct.unpack(">I", buf[off + 20 : off + 24])[0]
        envelope = buf[off + head_len : off + head_len + count]
        if len(envelope) != count:
            raise ValueError(
                f"PVDI payload truncated in {path_2ex}: {len(envelope)} < {count} bytes"
            )
        if envelope and max(envelope) > 4:
            raise ValueError(f"PVDI intensity > 4 in {path_2ex}: format changed")
        return _PVDI_RATE / _PVDI_HOP, envelope
    return None


def _vocal_regions(envelope: bytes, fps: float) -> list[dict[str, Any]]:
    """Runs of intensity >= VOCAL_INTENSITY_MIN, merged (< VOCAL_MERGE_GAP_S
    gaps), dropped when shorter than VOCAL_MIN_REGION_S; intensity = max in
    the merged run (SPIKE-B1 recipe + SPIKE-B2 calibrated params)."""
    runs: list[list[int]] = []
    start: int | None = None
    for i, value in enumerate(envelope):
        if value >= VOCAL_INTENSITY_MIN and start is None:
            start = i
        elif value < VOCAL_INTENSITY_MIN and start is not None:
            runs.append([start, i])
            start = None
    if start is not None:
        runs.append([start, len(envelope)])

    merged: list[list[int]] = []
    for run_start, run_end in runs:
        if merged and (run_start - merged[-1][1]) / fps < VOCAL_MERGE_GAP_S:
            merged[-1][1] = run_end
        else:
            merged.append([run_start, run_end])

    regions: list[dict[str, Any]] = []
    for run_start, run_end in merged:
        if (run_end - run_start) / fps < VOCAL_MIN_REGION_S:
            continue
        regions.append(
            {
                "start_s": round(run_start / fps, 2),
                "end_s": round(run_end / fps, 2),
                "intensity": int(max(envelope[run_start:run_end])),
            }
        )
    return regions


def _decode_vocals_payload(path_2ex: BytesSource) -> dict[str, Any]:
    """The three PVDI states, decoded from the file with no caching."""
    pvdi = read_pvdi(path_2ex)
    if pvdi is None:
        return {"status": "not_analyzed"}
    fps, envelope = pvdi
    regions = _vocal_regions(envelope, fps)
    if not regions:
        return {"status": "no_vocals", "fps": round(fps, 2), "regions": []}
    return {"status": "rekordbox", "fps": round(fps, 2), "regions": regions}


def vocals_payload(path_2ex: Path) -> dict[str, Any]:
    """The PVDI-derived ``vocals`` field for /anlz -- exactly one of the
    three PVDI states (SPIKE-SUMMARY section 3): rekordbox / no_vocals /
    not_analyzed. The demucs fallback (fourth status) is merged at serve
    time by :func:`merge_demucs_vocals`, never cached here.

    Results are cached per .2EX path and revalidated by source mtime, so a
    re-analyzed track decodes again on its next read. Each caller gets its
    own copy: rows are handed to serializers that must not be able to reach
    back into the cache. A malformed PVDI still raises out of
    :func:`read_pvdi` and is never cached."""
    if not path_2ex.is_file():
        return {"status": "not_analyzed"}
    key = str(path_2ex)
    mtime = path_2ex.stat().st_mtime
    hit = _VOCALS_CACHE.get(key, key, mtime)
    if hit is not None:
        return hit
    return _VOCALS_CACHE.put(key, key, mtime, _decode_vocals_payload(path_2ex))


def demucs_vocals_payload(content: RbContent) -> dict[str, Any] | None:
    """``{"status": "demucs", ...}`` from the vocal-cache, or None.

    None covers every real absence: no cache entry, schema-bumped entry,
    streaming/pathless track, audio gone from disk, or audio_mtime changed
    since analysis (regions computed for a different file must never be
    served - SPIKE-SUMMARY section 3 cache contract). Corrupt entries
    raise inside apps.vocals.cache - fail fast, no invented regions.
    """
    from apps.adapters.rekordbox import config
    from apps.adapters.rekordbox.paths import is_streaming_path, resolve_asset_path

    VOCAL_CACHE_DIR = config.VOCAL_CACHE_DIR

    if content.folder_path is None or is_streaming_path(content.folder_path):
        return None
    entry_path = VOCAL_CACHE_DIR / f"{content.stable_id}.json"
    if not entry_path.is_file():
        return None  # no entry: skip resolving the audio path it would be checked against
    mapped = resolve_asset_path(content.folder_path)
    if mapped.resolved is None:
        return None  # unmapped on this platform: audio is not locatable
    entry = vocal_cache.load_valid_entry(entry_path, mapped.resolved)
    if entry is None:
        return None
    return vocal_cache.anlz_vocals_of(entry)


def merge_demucs_vocals(payload: dict[str, Any], content: RbContent) -> dict[str, Any]:
    """Serve-time merge: when PVDI said not_analyzed, consult the demucs
    vocal-cache. Applied AFTER the anlz file cache on purpose -- the cached
    payload stays PVDI-only, so a vocal-cache entry landing (or being
    invalidated) later is reflected without an anlz-cache schema bump.
    After this merge, ``not_analyzed`` means NEITHER source exists."""
    if payload["vocals"]["status"] != "not_analyzed":
        return payload
    demucs = demucs_vocals_payload(content)
    if demucs is None:
        return payload
    return {**payload, "vocals": demucs}


def vocals_for_content(
    content: RbContent, *, resolver: AssetResolver | None = None
) -> dict[str, Any]:
    """Listing hot path: PVDI from .2EX, else demucs vocal-cache.

    Same four statuses as ``/anlz`` vocals. Used by :func:`build_track_rows`
    so library PreviewStrip blue bars do not need a per-row /anlz fetch.

    The PVDI half is mtime-cached by :func:`vocals_payload`; the demucs
    fallback below stays live so a vocal-cache entry landing later shows up
    on the next row hydration, per :func:`merge_demucs_vocals`.

    ``resolver``: see :func:`preview_strip`'s docstring (pin ad59ac) --
    same per-call memo, threaded through by the same caller.
    """
    from .row_assets import pvdi_vocals

    vocals = pvdi_vocals(content.analysis_data_path, resolver=resolver)
    if vocals["status"] != "not_analyzed":
        return vocals
    demucs = demucs_vocals_payload(content)
    return demucs if demucs is not None else vocals


# ----- ANLZ payload (waveform + beatgrid + phrases) ---------------------------
# The own-beatgrid overlay (native-analysis v1, NATIVE-01/02/03) that used to
# live here moved to own_beatgrid_overlay.py: it alone pushed this file over
# the 600-line size ceiling. Multi-anchor own maps are trusted there (flag
# omitted, dynamic_tempo set); see that module's docstring.


def _first_tags(directory: Path) -> tuple[dict[str, Any], list[str]]:
    """First occurrence of each tag type, plus the files that would not parse.

    Parses each ANLZ file SEPARATELY. ``read_anlz_files`` parses the set in one
    call, so a single unparseable sibling took the whole track's analysis down
    with a 500. A colour waveform tag can fail a construct check while
    sibling .DAT (PQTZ beatgrid, PWAV, PCOB cues) and .2EX (PWV6/PWV7
    tri-band) tags remain parseable.

    Losing one file is NOT the same as losing the analysis, so the parseable
    files are kept -- but the failure is RETURNED, never swallowed. The caller
    puts it in the payload so a client can say which lanes are missing instead
    of showing an empty waveform lane that looks like real silence.
    """
    from pyrekordbox.anlz import AnlzFile

    from apps.adapters.rekordbox.errors import not_found

    tags: dict[str, Any] = {}
    unreadable: list[str] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.upper() not in {".DAT", ".EXT", ".2EX"}:
            continue
        try:
            anlz_file = AnlzFile.parse_file(str(path))
        except Exception as exc:  # noqa: BLE001 -- one bad sibling must not 500 the track
            log.warning("ANLZ file %s is unparseable (%s)", path.name, exc)
            unreadable.append(path.name)
            continue
        for tag in anlz_file.tags:  # tags is a LIST in pyrekordbox 0.4.4
            tags.setdefault(tag.type, tag)
    if not tags:
        raise not_found(
            "ANALYSIS_NOT_FOUND",
            f"no ANLZ file in {directory} could be parsed: {unreadable}",
        )
    return tags, unreadable


def _phrases_payload(tags: dict[str, Any], times: list[float]) -> list[dict[str, Any]]:
    pssi = tags.get("PSSI")
    if pssi is None or not times:
        return []

    def time_of_beat(beat: int) -> float:
        idx = min(max(beat - 1, 0), len(times) - 1)
        return round(times[idx], 3)

    mood = int(pssi.content.mood)
    end_beat = int(pssi.content.end_beat)
    entries = list(pssi.content.entries)
    phrases: list[dict[str, Any]] = []
    for i, entry in enumerate(entries):
        start_beat = int(entry.beat)
        stop_beat = int(entries[i + 1].beat) if i + 1 < len(entries) else end_beat
        phrases.append(
            {
                "start_s": time_of_beat(start_beat),
                "end_s": time_of_beat(stop_beat),
                "kind": int(entry.kind),
                "mood": mood,
            }
        )
    return phrases


def build_anlz_payload(
    content: RbContent, points: int, state_db_path: Path | None = None
) -> dict[str, Any]:
    """Parse ANLZ + djmdCue into the COMPONENT-MAP 2.3 JSON, with file cache.

    Cache: data/state/anlz-cache/{stable_id}.json keyed on (schema version,
    anlz file mtime, points); any mismatch recomputes and rewrites, so old
    unversioned or stale-schema entries self-heal. Cues are always overlaid from
    the live ``djmdCue`` rows because they can change without touching ANLZ files.

    ``state_db_path`` is forwarded to ``apply_own_overlays`` (Codex P2
    BLOCKING, PR #1587): a caller with an app-configured analysis database
    passes it through so every own-lane overlay reads the same database the
    selection toggle was read from, not the process-global default.
    """
    from apps.adapters.rekordbox.errors import not_found
    from apps.adapters.rekordbox.paths import (
        _asset_sibling,
        anlz_dir,
        resolve_asset_path,
    )

    from .anlz_cache import _anlz_mtime, _load_cached_payload, _store_cached_payload
    from .beatgrid_issue_cache import _ensure_beatgrid_issue_cached
    from .db import fetch_cues
    from .lead_in_shift import anlz_lead_in_s, on_our_timeline

    directory = anlz_dir(content)
    # anlz_dir() already resolved + verified AnalysisDataPath (mapped and
    # on disk); the .2EX sibling (PVDI carrier) shares its stem.
    assert content.analysis_data_path is not None
    mapped_adp = resolve_asset_path(content.analysis_data_path)
    assert mapped_adp.resolved is not None  # anlz_dir() would have 404'd
    mapped_twoex = _asset_sibling(mapped_adp, mapped_adp.resolved.with_suffix(".2EX"))
    if mapped_twoex.resolved is None:
        raise not_found(
            "ANALYSIS_NOT_FOUND",
            f".2EX file for track {content.stable_id} is unsafe "
            f"({mapped_twoex.reason}): {content.analysis_data_path}",
        )
    twoex_path = mapped_twoex.resolved
    try:
        dat_mtime: float | None = mapped_adp.resolved.stat().st_mtime
    except OSError:
        # anlz_dir() already verified the real directory exists; only a
        # mocked/synthetic path (unit tests) lands here. Skip the sidecar
        # cache write rather than fail the whole payload over it.
        dat_mtime = None
    anlz_mtime = _anlz_mtime(directory)
    cached = _load_cached_payload(content.stable_id, anlz_mtime, points)
    if cached is not None:
        if dat_mtime is not None:
            _ensure_beatgrid_issue_cached(
                content.stable_id, dat_mtime, cached["beatgrid"]["beats"]
            )
        payload = on_our_timeline(dict(cached), anlz_lead_in_s(content.folder_path))
        payload["cues"] = fetch_cues(content.vendor_id)
        return apply_own_overlays(
            merge_demucs_vocals(payload, content), content.stable_id, state_db_path
        )

    tags, unreadable_anlz = _first_tags(directory)
    if "PWV6" in tags and "PWV7" in tags:
        kind = "tri"
        preview_bands = _tri_bands(tags["PWV6"])
        detail_bands = _tri_bands(tags["PWV7"])
    else:
        kind = "mono"
        preview_bands = _mono_bands(tags["PWAV"]) if "PWAV" in tags else {}
        detail_bands = _mono_bands(tags["PWV3"]) if "PWV3" in tags else {}
    empty_bands = {"length": 0, "low": [], "mid": [], "high": []}
    beatgrid, times = _beatgrid_payload(tags)
    if dat_mtime is not None:
        _ensure_beatgrid_issue_cached(content.stable_id, dat_mtime, beatgrid["beats"])
    payload: dict[str, Any] = {
        "stable_id": content.stable_id,
        "points": points,
        "waveform": {
            "kind": kind,
            "preview": _bands_payload(preview_bands, points)
            if preview_bands
            else dict(empty_bands),
            "detail": _bands_payload(detail_bands, points)
            if detail_bands
            else dict(empty_bands),
        },
        "beatgrid": beatgrid,
        "phrases": _phrases_payload(tags, times),
        # contract item 5: PVDI-derived vocal regions, three explicit states.
        "vocals": vocals_payload(twoex_path),
        # ANLZ files rekordbox wrote but pyrekordbox cannot parse. Empty for
        # a healthy track. Non-empty means some lanes below are absent because
        # their tags were unreadable -- NOT because the track has no such data.
        "unreadable_anlz": unreadable_anlz,
    }

    _store_cached_payload(content.stable_id, anlz_mtime, points, payload)
    payload = on_our_timeline(payload, anlz_lead_in_s(content.folder_path))
    payload["cues"] = fetch_cues(content.vendor_id)
    return apply_own_overlays(
        merge_demucs_vocals(payload, content), content.stable_id, state_db_path
    )
