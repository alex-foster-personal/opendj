"""Python twin of lighting stage replay query (LIGHT-10573, issue #9898).

Mirrors ``frames-client.ts`` (`parseReplayFile`, `openReplay`, `frameAt`) and
``stage-session.ts`` (`stageQuery`). Pinned by ``tests/lighting/fixtures/stage-replay-vectors.json``.
"""

from __future__ import annotations

import json
import math
from typing import Any


def _clamp01(v: float) -> float:
    if v < 0:
        return 0.0
    if v > 1:
        return 1.0
    return v


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _parse_rgb(raw: object, path: str) -> list[int]:
    if isinstance(raw, str):
        s = raw.strip()
        if len(s) == 7 and s[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in s[1:]):
            return [int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16)]
        raise ValueError(f"{path}: expected #rrggbb hex string")
    if not isinstance(raw, list) or len(raw) != 3:
        raise ValueError(f"{path}: expected [r,g,b] with three numbers 0..255")
    out: list[int] = []
    for i, v in enumerate(raw):
        if not _is_number(v):
            raise ValueError(f"{path}[{i}]: expected a number")
        c = float(v)
        if c < 0:
            c = 0.0
        if c > 255:
            c = 255.0
        out.append(int(c))
    return out


def _parse_fixture_state(raw: object, index: int) -> dict[str, Any]:
    path = f"fixtures[{index}]"
    if not isinstance(raw, dict):
        raise TypeError(f"{path}: expected object")
    obj = raw
    fid = obj.get("id")
    if not isinstance(fid, str) or fid == "":
        raise ValueError(f"{path}.id: expected non-empty string")
    rgb = _parse_rgb(obj.get("rgb"), f"{path}.rgb")
    intensity_raw = obj.get("intensity")
    if not _is_number(intensity_raw):
        raise ValueError(f"{path}.intensity: expected a number")
    pan_raw = obj.get("pan_deg")
    if pan_raw is None:
        pan_deg = None
    elif _is_number(pan_raw):
        pan_deg = float(pan_raw)
    else:
        raise ValueError(f"{path}.pan_deg: expected number or null")
    tilt_raw = obj.get("tilt_deg")
    if tilt_raw is None:
        tilt_deg = None
    elif _is_number(tilt_raw):
        tilt_deg = float(tilt_raw)
    else:
        raise ValueError(f"{path}.tilt_deg: expected number or null")
    strobe_raw = obj.get("strobe_hz")
    if strobe_raw is None:
        strobe_hz = 0.0
    elif _is_number(strobe_raw):
        strobe_hz = float(strobe_raw)
    else:
        raise ValueError(f"{path}.strobe_hz: expected a number")
    return {
        "id": fid,
        "rgb": rgb,
        "intensity": _clamp01(float(intensity_raw)),
        "pan_deg": pan_deg,
        "tilt_deg": tilt_deg,
        "strobe_hz": strobe_hz,
    }


def parse_frame_message(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TypeError("frame: expected object")
    t_ns = raw.get("t_ns")
    if not _is_number(t_ns):
        raise ValueError("t_ns: expected a finite number")
    fixtures_raw = raw.get("fixtures")
    if not isinstance(fixtures_raw, list):
        raise TypeError("fixtures: expected array")
    patch_version = raw.get("patch_version")
    if patch_version is not None and not isinstance(patch_version, (str, int, float)):
        patch_version = None
    fixtures = [_parse_fixture_state(item, i) for i, item in enumerate(fixtures_raw)]
    return {"t_ns": float(t_ns), "patch_version": patch_version, "fixtures": fixtures}


def _read_position(obj: dict[str, Any], path: str) -> tuple[float, float]:
    pos = obj.get("position")
    if isinstance(pos, list) and len(pos) >= 2:
        x, y = pos[0], pos[1]
        if _is_number(x) and _is_number(y):
            return float(x), float(y)
        raise ValueError(f"{path}.position: expected [x, y] numbers")
    if isinstance(pos, dict):
        x, y = pos.get("x"), pos.get("y")
        if _is_number(x) and _is_number(y):
            return float(x), float(y)
        raise ValueError(f"{path}.position: expected {{ x, y }} numbers")
    x, y = obj.get("x"), obj.get("y")
    if _is_number(x) and _is_number(y):
        return float(x), float(y)
    raise ValueError(f"{path}: expected x/y or position")


def _parse_stage_fixture(raw: object, index: int) -> dict[str, Any]:
    path = f"fixtures[{index}]"
    if not isinstance(raw, dict):
        raise TypeError(f"{path}: expected object")
    fid = raw.get("id")
    if not isinstance(fid, str) or fid == "":
        raise ValueError(f"{path}.id: expected non-empty string")
    x, y = _read_position(raw, path)
    groups_raw = raw.get("groups")
    groups = [g for g in groups_raw if isinstance(g, str)] if isinstance(groups_raw, list) else []
    out: dict[str, Any] = {"id": fid, "x": x, "y": y, "groups": groups}
    if _is_number(raw.get("pan_range_deg")):
        out["pan_range_deg"] = float(raw["pan_range_deg"])
    if _is_number(raw.get("tilt_range_deg")):
        out["tilt_range_deg"] = float(raw["tilt_range_deg"])
    return out


def _parse_patch(raw: object) -> dict[str, Any]:
    if isinstance(raw, list):
        return {"version": None, "fixtures": [_parse_stage_fixture(item, i) for i, item in enumerate(raw)]}
    if not isinstance(raw, dict):
        raise TypeError("patch: expected object or array")
    fixtures_raw = raw.get("fixtures")
    if not isinstance(fixtures_raw, list):
        raise TypeError("patch.fixtures: expected array")
    version = raw.get("version")
    if version is not None and not isinstance(version, (str, int, float)):
        version = None
    return {
        "version": version,
        "fixtures": [_parse_stage_fixture(item, i) for i, item in enumerate(fixtures_raw)],
    }


def parse_replay_text(text: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("replay file: JSON parse failed") from None
    patch: dict[str, Any] | None = None
    frames_raw: list[object]
    if isinstance(parsed, list):
        frames_raw = parsed
    elif isinstance(parsed, dict):
        fr = parsed.get("frames")
        if not isinstance(fr, list):
            raise TypeError("replay file: frames must be a non-empty array")
        frames_raw = fr
        if "patch" in parsed and parsed["patch"] is not None:
            patch = _parse_patch(parsed["patch"])
    else:
        raise TypeError("replay file: expected array or { frames, patch? }")
    if len(frames_raw) == 0:
        raise ValueError("replay file: frame list is empty")
    frames = [parse_frame_message(item) for item in frames_raw]
    indexed = list(enumerate(frames))
    indexed.sort(key=lambda pair: (pair[1]["t_ns"], pair[0]))
    return patch, [f for _, f in indexed]


def frame_at(frames: list[dict[str, Any]], elapsed_s: float) -> tuple[dict[str, Any], int]:
    if len(frames) == 1:
        return frames[0], 0
    t0 = frames[0]["t_ns"]
    times_s = [(f["t_ns"] - t0) / 1e9 for f in frames]
    last = len(frames) - 1
    if elapsed_s <= times_s[0]:
        return frames[0], 0
    if elapsed_s >= times_s[last]:
        return frames[last], last
    lo, hi = 0, last
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if times_s[mid] <= elapsed_s:
            lo = mid
        else:
            hi = mid - 1
    return frames[lo], lo


def replay_query(text: str, *, at_s: float = 0.0, name: str | None = None) -> dict[str, Any]:
    patch, frames = parse_replay_text(text)
    t0 = frames[0]["t_ns"]
    t_last = frames[-1]["t_ns"]
    duration_s = max(0.0, (t_last - t0) / 1e9)
    elapsed = at_s
    if elapsed < 0:
        elapsed = 0.0
    elapsed = min(elapsed, duration_s)
    frame, index = frame_at(frames, elapsed)
    patch_source: str | None
    if patch is not None:
        patch_source = "replay"
        active_patch = patch
    else:
        patch_source = None
        active_patch = None
    patch_version = active_patch["version"] if active_patch else None
    patch_fixture_count = len(active_patch["fixtures"]) if active_patch else 0
    return {
        "version": 1,
        "mode": "replay",
        "playing": False,
        "elapsed_s": elapsed,
        "duration_s": duration_s,
        "replay_name": name,
        "frame_count": len(frames),
        "frame_index": index,
        "frame": frame,
        "patch_source": patch_source,
        "patch_version": patch_version,
        "patch_fixture_count": patch_fixture_count,
        "last_error": None,
    }
