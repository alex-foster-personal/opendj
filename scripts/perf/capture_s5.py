"""S5 deck-load HTTP fetch wall: GET /anlz + /audio against a running engine.

Pinned CFG stable_ids are the honest denominator: only tracks whose /audio
200s. Empty CFG (no live library in this worktree) fail-fasts unless the
caller passes ``--track-*``. This capture is the HTTP fetch wall a deck load
issues (fetchAnlz + fetchAudioArrayBuffer), not decodeMix.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from scripts.perf.capture_kpi_ledger import (
    ANLZ_TIMEOUT_S,
    AUDIO_TIMEOUT_S,
    S5_METHOD,
    S5_REQUIRED,
    S5_UNIT,
    CaptureMeta,
    build_row,
    error_row,
    fetch_url,
    withheld_row,
)

CFG: dict[str, Any] = {
    "points": 38400,  # fetchAnlz default in api-rb.ts
    "runs": 5,
    "tracks": {
        # Pin after a live engine whose /audio 200'd. Empty = fail-fast.
        # Override with --track-small / --track-large / --track-stemmed.
        "small": "",  # smallest resolvable audio; bytes/duration unknown here
        "large": "",  # largest resolvable audio
        "stemmed": "",  # resolvable audio AND GET /stems answers a manifest with parts
    },
}

ROLES = ("small", "large", "stemmed")


class ProbeError(RuntimeError):
    """A track probe could not measure. Never coerced to a number."""


def headline_ms(per_track_totals: list[list[float]]) -> float:
    """Max of per-track medians (the map's MAX-of-N-loads caveat)."""
    return max(statistics.median(runs) for runs in per_track_totals)


def median_ms(runs: list[float]) -> float:
    return float(statistics.median(runs))


def resolve_tracks(
    *,
    small: str | None,
    large: str | None,
    stemmed: str | None,
) -> dict[str, str]:
    cfg = CFG["tracks"]
    return {
        "small": small if small is not None else str(cfg["small"]),
        "large": large if large is not None else str(cfg["large"]),
        "stemmed": stemmed if stemmed is not None else str(cfg["stemmed"]),
    }


def _anlz_cache_state(data_dir: Path | None, stable_id: str) -> str | None:
    if data_dir is None:
        return None
    path = data_dir / "state" / "anlz-cache" / f"{stable_id}.json"
    return "hit" if path.is_file() else "miss"


def _timed_get(url: str, timeout: float) -> tuple[float, int, Any, bytes]:
    t0 = time.perf_counter()
    status, headers, body = fetch_url(url, timeout)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return elapsed_ms, status, headers, body


def _content_type(headers: Any) -> str:
    if headers is None:
        return ""
    raw = headers.get("Content-Type") or headers.get("content-type") or ""
    return str(raw).split(";", 1)[0].strip().lower()


def _detail_code(body: bytes) -> str:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    detail = parsed.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("code") or "")
    return ""


def _require_audio(status: int, headers: Any, body: bytes, stable_id: str) -> None:
    ctype = _content_type(headers)
    if status == 200 and ctype.startswith("audio/"):
        return
    code = _detail_code(body)
    if code in {"TRACK_NOT_FOUND", "AUDIO_FILE_MISSING"}:
        raise ProbeError(f"{code} for {stable_id} (HTTP {status})")
    raise ProbeError(f"/audio for {stable_id} returned HTTP {status} content-type={ctype!r}")


def _require_anlz(status: int, body: bytes, stable_id: str) -> None:
    if status != 200:
        raise ProbeError(f"/anlz for {stable_id} returned HTTP {status}")
    try:
        json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeError(f"/anlz for {stable_id} was not JSON: {exc}") from exc


def require_stem_bundle(engine: str, stable_id: str) -> None:
    """Prove the stemmed role has a stored bundle: a manifest with parts (#3966).

    ``GET /stems`` answers HTTP 200 for a track WITHOUT stems too, with body
    ``{"status": "unavailable", "code": "STEM_BUNDLE_NOT_FOUND"}``, so the
    status code alone admitted any track. Only a manifest for this stable_id
    that lists at least one part counts; anything else raises.
    """
    status, _headers, body = fetch_url(
        f"{engine}/api/v1/tracks/{stable_id}/stems", ANLZ_TIMEOUT_S,
    )
    if status != 200:
        raise ProbeError(f"GET /stems for {stable_id} returned HTTP {status}")
    try:
        manifest = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeError(f"GET /stems for {stable_id} was not JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ProbeError(f"GET /stems for {stable_id} was not a JSON object")
    if manifest.get("status") == "unavailable":
        raise ProbeError(
            f"stemmed track {stable_id} has no stem bundle: "
            f"{manifest.get('code') or 'unavailable'} (HTTP 200 unavailable)"
        )
    parts = manifest.get("parts")
    if manifest.get("stable_id") != stable_id or not isinstance(parts, dict) or not parts:
        raise ProbeError(
            f"GET /stems for {stable_id} is not a stem manifest for that track: "
            f"stable_id={manifest.get('stable_id')!r} parts={parts!r}"
        )


def _content_length(headers: Any, body: bytes) -> int:
    if headers is not None:
        raw = headers.get("Content-Length") or headers.get("content-length")
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
    return len(body)


def _label_run(cache_before: str | None, index: int) -> str | None:
    if cache_before == "hit":
        return "warm"
    if cache_before == "miss":
        return "cold" if index == 0 else "warm"
    return None if index == 0 else "warm"


def _probe_track(
    engine: str,
    role: str,
    stable_id: str,
    *,
    points: int,
    runs: int,
    data_dir: Path | None,
) -> dict[str, Any]:
    if role == "stemmed":
        require_stem_bundle(engine, stable_id)
    cache_before = _anlz_cache_state(data_dir, stable_id)
    anlz_url = f"{engine}/api/v1/tracks/{stable_id}/anlz?points={points}"
    audio_url = f"{engine}/api/v1/tracks/{stable_id}/audio"
    anlz_ms: list[float] = []
    audio_ms: list[float] = []
    audio_bytes = 0
    for index in range(runs):
        elapsed, status, _headers, body = _timed_get(anlz_url, ANLZ_TIMEOUT_S)
        _require_anlz(status, body, stable_id)
        anlz_ms.append(elapsed)
        elapsed, status, headers, body = _timed_get(audio_url, AUDIO_TIMEOUT_S)
        _require_audio(status, headers, body, stable_id)
        audio_ms.append(elapsed)
        if index == 0:
            audio_bytes = _content_length(headers, body)
    totals = [
        (_label_run(cache_before, index), anlz + audio)
        for index, (anlz, audio) in enumerate(zip(anlz_ms, audio_ms, strict=True))
    ]
    return {
        "role": role,
        "stable_id": stable_id,
        "bytes": audio_bytes,
        "anlz_cache": cache_before,
        "totals": totals,
    }


def _bucket(results: list[dict[str, Any]], state: str) -> list[list[float]]:
    buckets: list[list[float]] = []
    for result in results:
        runs = [ms for label, ms in result["totals"] if label == state]
        if runs:
            buckets.append(runs)
    return buckets


def _ids_note(results: list[dict[str, Any]]) -> str:
    parts = [f"{item['role']}={item['stable_id']}:{item['bytes']}B" for item in results]
    return " ".join(parts)


def _state_note(state: str, results: list[dict[str, Any]], n: int, points: int) -> str:
    cache_label = "miss" if state == "cold" else "hit"
    return (
        f"{state}; n={n}; points={points}; anlz_cache={cache_label}; "
        f"audio=os-page-cache; HTTP fetch wall not decodeMix; {_ids_note(results)}"
    )


def _empty_cfg_reason(tracks: dict[str, str]) -> str | None:
    missing = [role for role in ROLES if not tracks.get(role)]
    if not missing:
        return None
    return "; ".join(f"CFG track {role} is empty" for role in missing)


def capture(
    *,
    engine: str,
    meta: CaptureMeta,
    tracks: dict[str, str],
    data_dir: Path | None,
) -> list[dict[str, Any]]:
    empty = _empty_cfg_reason(tracks)
    if empty:
        return [
            error_row(
                kpi=S5_REQUIRED[0],
                unit=S5_UNIT,
                method=S5_METHOD,
                meta=meta,
                reason=empty,
            )
        ]
    points = int(CFG["points"])
    runs = int(CFG["runs"])
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for role in ROLES:
        try:
            results.append(
                _probe_track(
                    engine,
                    role,
                    tracks[role],
                    points=points,
                    runs=runs,
                    data_dir=data_dir,
                )
            )
        except (ProbeError, OSError) as exc:
            failures.append(f"{role}: {exc}")
    if failures:
        return [
            error_row(
                kpi=S5_REQUIRED[0],
                unit=S5_UNIT,
                method=S5_METHOD,
                meta=meta,
                reason="; ".join(failures),
            )
        ]
    rows: list[dict[str, Any]] = []
    cold_buckets = _bucket(results, "cold")
    warm_buckets = _bucket(results, "warm")
    if cold_buckets:
        cold_value = headline_ms(cold_buckets)
        cold_n = len(cold_buckets[0])
        note = _state_note("cold", results, cold_n, points)
        rows.append(
            build_row(
                kpi="packaged_deck_load_total_ms",
                value=cold_value,
                unit=S5_UNIT,
                method=S5_METHOD,
                meta=meta,
                note=note,
            )
        )
        rows.append(
            build_row(
                kpi="boot_window_deck_load_fetchwall_ms",
                value=cold_value,
                unit=S5_UNIT,
                method=S5_METHOD,
                meta=meta,
                note=note,
            )
        )
    if warm_buckets:
        warm_value = headline_ms(warm_buckets)
        warm_n = max(len(bucket) for bucket in warm_buckets)
        note = _state_note("warm", results, warm_n, points)
        rows.append(
            build_row(
                kpi="packaged_deck_load_total_ms",
                value=warm_value,
                unit=S5_UNIT,
                method=S5_METHOD,
                meta=meta,
                note=note,
            )
        )
        rows.append(
            build_row(
                kpi="settled_deck_load_fetchwall_ms_ab",
                value=warm_value,
                unit=S5_UNIT,
                method=S5_METHOD,
                meta=meta,
                note=note,
            )
        )
    rows.append(
        withheld_row(
            kpi="packaged_decode_throughput_mb_s",
            unit="MB/s",
            method=S5_METHOD,
            meta=meta,
            reason="withheld: this capture has no decodeMix",
        )
    )
    if not cold_buckets and not warm_buckets:
        return [
            error_row(
                kpi=S5_REQUIRED[0],
                unit=S5_UNIT,
                method=S5_METHOD,
                meta=meta,
                reason="no S5 run could be tagged cold or warm",
            )
        ]
    return rows
