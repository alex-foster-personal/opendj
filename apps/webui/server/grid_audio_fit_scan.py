"""Judge the library's beatgrids against their audio (GRIDFIT-02).

The grid judged is the deck's grid for v1, rekordbox's PQTZ (read exactly as
`grid_quality_scan` reads it). The audio is the track's file on this machine,
decoded by the shipped `odj-audio` (`engine_decode.decode_f32`), so the
verdict is the same under the Tauri and Electron shells. Off-beat is judged
only when the in-house Beat This! analysis left its activations behind.

COST. About 1 s of one core per track (decode 0.15 to 0.4 s, judge 0.5 to
1.5 s, measured Tue 27 Oct 2026 on 3 to 8 minute tracks) and 140 to 280 MB
peak, so this is never part of a listing: it runs on its own thread, started
by `POST /beatgrid-flags/audio-fit/scan` or the CLI, and incrementally: a
track whose grid file, audio file and rule are unchanged is skipped after two
`stat`s. `judge_track` does one track on demand (a deck load).

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 judge_track(): verdict for one track, stored.
    [if] the track has no rekordbox grid or no audio here [then] unknown with
      that reason ⛔️ a guessed verdict or a crash
    [if] the decoder cannot read the file [then] unknown, decode_failed ⛔️ fit
    [if] the rule returns a reason [then] the stored row carries it ⛔️ reason null on a judged fit
  ✔︎ ✅ 🎯 scan_library(): every present track, incremental.
    [if] nothing changed since the last scan [then] nothing is decoded ⛔️
    [if] the audio file changed [then] re-judged ⛔️ a stale verdict

-Claude
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from apps.adapters.rekordbox import config
from apps.adapters.rekordbox.errors import _open_ro
from apps.analysis_beatgrid.activations import ActivationsMissing, load_activations
from apps.analysis_beatgrid.grid_audio_fit import GridAudioFit, GridBeat, judge_grid_against_audio, rule_version
from apps.shared.engine_decode import EngineDecodeFailed, EngineDecoderUnavailable, decode_f32
from apps.shared.platform_paths import AssetResolver
from apps.webui.server import grid_audio_fit_store, library_playable
from apps.webui.server.grid_audio_fit_store import StoredGridAudioFit
from apps.webui.server.grid_quality_scan import ScanResult, ScanRunner, _grid_identity, read_pqtz_grid
from apps.webui.server.rb_vendor_pkg.lead_in_shift import anlz_lead_in_s
from apps.webui.server.rb_vendor_pkg.track_rows import bulk_rb_meta

log = logging.getLogger(__name__)

REASON_NO_GRID = "no_rekordbox_grid"
REASON_GRID_UNREADABLE = "grid_unreadable"
REASON_NO_AUDIO = "audio_not_on_this_machine"
REASON_DECODE_FAILED = "decode_failed"
REASON_DECODER_UNAVAILABLE = "decoder_unavailable"

BACKGROUND_PAUSE_S = 0.05
WRITE_BATCH = 20
SCAN_THREAD_NAME = "grid-audio-fit-scan"
_OWN_BEATGRID_BACKEND = "own_beatgrid.backfill"


@dataclass(frozen=True)
class _Subject:
    stable_id: str
    grid_version: str
    dat_path: Path | None
    grid_reason: str | None
    audio_path: Path | None
    audio_version: str
    #: rekordbox's time zero minus ours for an MP3 (`apps.shared.mp3_lead_in`):
    #: the deck plays the grid with this taken off, so the judge must too.
    lead_in_s: float


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _audio_version(path: Path | None) -> str:
    if path is None:
        return "none"
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    path_id = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    return f"file:{path_id}:{stat.st_mtime_ns}:{stat.st_size}"


def _present_paths(stable_ids: list[str] | None) -> dict[str, Path]:
    """Local audio for the given ids (one lookup), or for the whole present library."""
    conn = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        if stable_ids is not None:
            return library_playable.local_audio_paths(conn, stable_ids)
        return {sid: Path(path) for sid, path in library_playable.scan_playability(conn).present}
    finally:
        conn.close()


def _subjects(stable_ids: list[str] | None) -> list[_Subject]:
    paths = _present_paths(stable_ids)
    ids = list(paths) if stable_ids is None else list(stable_ids)
    metas = bulk_rb_meta(ids)
    resolver = AssetResolver()
    out = []
    for sid in ids:
        _source, grid_version, dat_path, grid_reason = _grid_identity(metas.get(sid), resolver)
        audio = paths.get(sid)
        meta = metas.get(sid)
        lead_in = anlz_lead_in_s(meta.folder_path if meta is not None else None) if dat_path is not None else 0.0
        out.append(_Subject(sid, grid_version, dat_path, grid_reason, audio, _audio_version(audio), lead_in))
    return out


def _beat_logits(stable_id: str, resolver: AssetResolver) -> tuple[np.ndarray, float] | None:
    """Beat This! logits for the track when the in-house analysis kept them."""
    conn = _open_ro(config.STATE_DB, "STATE_DB")
    try:
        # The analysis table is created by the in-house analyzer on first use; a
        # library never analyzed has none, which means "no activations", not an error.
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis'").fetchone() is None:
            return None
        # Several (stable_id, backend, backend_version) rows are legal; pick the newest.
        row = conn.execute(
            "SELECT record_json FROM analysis"
            " WHERE stable_id = ? AND backend = ?"
            " ORDER BY analyzed_at DESC, backend_version DESC"
            " LIMIT 1",
            (stable_id, _OWN_BEATGRID_BACKEND),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    ref = (json.loads(row[0]).get("features_blob") or {}).get("activations") or {}
    npz = ref.get("npz")
    if not npz:
        return None
    mapped = resolver.resolve_asset_path(npz).resolved
    if mapped is None:
        return None
    try:
        arrays = load_activations(npz=str(mapped))
    except ActivationsMissing:
        return None
    return arrays["beat"], float(arrays["fps"])


def _row(subject: _Subject, rule: str, fit: GridAudioFit | None, reason: str | None) -> StoredGridAudioFit:
    return StoredGridAudioFit(
        stable_id=subject.stable_id,
        grid_version=subject.grid_version,
        audio_version=subject.audio_version,
        rule_version=rule,
        verdict=fit.verdict if fit is not None else "unknown",
        reason=reason,
        grid_bpm=fit.grid_bpm if fit else None,
        audio_bpm=fit.audio_bpm if fit else None,
        fold_sharpness_audio=fit.fold_sharpness_audio if fit else None,
        fold_sharpness_grid=fit.fold_sharpness_grid if fit else None,
        kick_offset_ms=fit.kick_offset_ms if fit else None,
        kick_spread_ms=fit.kick_spread_ms if fit else None,
        kick_coverage=fit.kick_coverage if fit else None,
        onbeat_ratio=fit.onbeat_ratio if fit else None,
        computed_at=_utc_now(),
    )


def _deck_grid(subject: _Subject, dat_path: Path) -> tuple[list[GridBeat], None] | tuple[None, str]:
    """The grid the deck plays, or why there is none.

    rekordbox's times with the MP3 lead-in taken off, exactly as `/anlz`
    serves it (`lead_in_shift.on_our_timeline`).
    """
    try:
        grid = read_pqtz_grid(dat_path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        log.warning("grid-audio-fit: unreadable ANLZ %s (%s)", dat_path, exc)
        return None, REASON_GRID_UNREADABLE
    if grid is None:
        return None, REASON_NO_GRID
    beats = [
        GridBeat(t=round(t - subject.lead_in_s, 3), bpm=bpm)
        for t, bpm in zip(grid[0], grid[1], strict=True)
        if t - subject.lead_in_s >= 0
    ]
    if len(beats) < 2:
        return None, REASON_NO_GRID
    return beats, None


def _judge(subject: _Subject, rule: str, resolver: AssetResolver) -> StoredGridAudioFit:
    if subject.dat_path is None:
        return _row(subject, rule, None, subject.grid_reason or REASON_NO_GRID)
    if subject.audio_path is None or subject.audio_version == "missing":
        return _row(subject, rule, None, REASON_NO_AUDIO)
    beats, reason = _deck_grid(subject, subject.dat_path)
    if beats is None:
        return _row(subject, rule, None, reason)
    try:
        raw, sr, _channels = decode_f32(subject.audio_path, mono=True)
    except EngineDecoderUnavailable:
        raise  # the engine itself is broken: stop, do not store a verdict per track
    except EngineDecodeFailed as exc:
        log.warning("grid-audio-fit: %s", exc)
        return _row(subject, rule, None, REASON_DECODE_FAILED)
    logits = _beat_logits(subject.stable_id, resolver)
    fit = judge_grid_against_audio(
        np.frombuffer(raw, dtype="<f4"),
        sr,
        beats,
        beat_logits=logits[0] if logits else None,
        logits_fps=logits[1] if logits else 50.0,
    )
    return _row(subject, rule, fit, fit.reason)


def judge_track(stable_id: str, *, force: bool = False) -> StoredGridAudioFit:
    """The current verdict for one track, judging it now if the stored one is stale."""
    rule = rule_version()
    subject = _subjects([stable_id])[0]
    previous = grid_audio_fit_store.read_many([stable_id]).get(stable_id)
    if (
        not force
        and previous is not None
        and (previous.grid_version, previous.audio_version, previous.rule_version)
        == (subject.grid_version, subject.audio_version, rule)
    ):
        return previous
    row = _judge(subject, rule, AssetResolver())
    grid_audio_fit_store.upsert_many([row])
    return row


def scan_library(*, scope: str = "present", pause_s: float = 0.0) -> ScanResult:
    """Bring the stored verdict of every present track up to date."""
    if scope != "present":
        raise ValueError("grid-audio-fit scans only tracks whose audio is on this machine (scope 'present')")
    started = time.perf_counter()
    rule = rule_version()
    subjects = _subjects(None)
    stored = grid_audio_fit_store.read_many([s.stable_id for s in subjects])
    resolver = AssetResolver()
    pending: list[StoredGridAudioFit] = []
    judged = unchanged = written = 0
    for subject in subjects:
        previous = stored.get(subject.stable_id)
        if previous is not None and (previous.grid_version, previous.audio_version, previous.rule_version) == (
            subject.grid_version,
            subject.audio_version,
            rule,
        ):
            unchanged += 1
            continue
        pending.append(_judge(subject, rule, resolver))
        judged += 1
        if pause_s > 0:
            time.sleep(pause_s)
        if len(pending) >= WRITE_BATCH:
            grid_audio_fit_store.upsert_many(pending)
            written += len(pending)
            pending = []
    grid_audio_fit_store.upsert_many(pending)
    written += len(pending)
    return ScanResult(
        scope="present",
        considered=len(subjects),
        parsed=judged,
        unchanged=unchanged,
        written=written,
        duration_s=round(time.perf_counter() - started, 3),
        finished_at=_utc_now(),
    )


_RUNNER_GUARD = threading.Lock()


def runner_for(app_state: Any) -> ScanRunner:
    """The one audio-fit runner of a FastAPI app (`app.state`)."""
    with _RUNNER_GUARD:
        runner = getattr(app_state, "grid_audio_fit_runner", None)
        if runner is None:
            runner = ScanRunner(scan_library, thread_name=SCAN_THREAD_NAME, label="grid-audio-fit")
            app_state.grid_audio_fit_runner = runner
        return runner


__all__ = [
    "BACKGROUND_PAUSE_S",
    "SCAN_THREAD_NAME",
    "judge_track",
    "runner_for",
    "scan_library",
]
