"""Invented tracks for the AutoDJ tests, shaped like the real ``/anlz`` payload.

No owner-library data: every id, title and number here is made up (board
privacy rule). Each track is a dance-music arrangement on a steady grid,
described as (label kind, bars) phrases in rekordbox's High mood, with
per-bar energy, bass and vocals that follow the arrangement, so a test can
predict what a window or a clash check should find.
"""

from __future__ import annotations

from typing import Any

from apps.autodj.profile import TrackProfile, profile_from_payloads

# rekordbox High-mood PSSI kinds (apps/autodj/sections.py).
INTRO, UP, DOWN, CHORUS, OUTRO = 1, 2, 3, 5, 6

#: (kind, bars, energy 0..1, bass 0..1, vocals?)
DEFAULT_ARRANGEMENT: tuple[tuple[int, int, float, float, bool], ...] = (
    (INTRO, 16, 0.4, 0.2, False),
    (UP, 16, 0.6, 0.5, False),
    (CHORUS, 32, 1.0, 1.0, True),
    (DOWN, 16, 0.3, 0.1, False),
    (UP, 16, 0.6, 0.5, False),
    (CHORUS, 16, 1.0, 1.0, True),
    (OUTRO, 16, 0.4, 0.8, False),
)

POINTS_PER_BAR = 8


def anlz_payload(
    stable_id: str,
    *,
    bpm: float = 128.0,
    arrangement: tuple[tuple[int, int, float, float, bool], ...] = DEFAULT_ARRANGEMENT,
    lead_in_s: float = 0.0,
    vocals_analyzed: bool = True,
) -> dict[str, Any]:
    beat_s = 60.0 / bpm
    total_bars = sum(bars for _, bars, *_ in arrangement)
    beats = [
        {"n": (index % 4) + 1, "bpm": bpm, "t": round(lead_in_s + index * beat_s, 3)}
        for index in range(total_bars * 4)
    ]
    phrases = []
    low: list[float] = []
    mid: list[float] = []
    high: list[float] = []
    regions = []
    bar = 0
    for kind, bars, energy, bass, vocals in arrangement:
        start_s = lead_in_s + bar * 4 * beat_s
        end_s = lead_in_s + (bar + bars) * 4 * beat_s
        phrases.append({"start_s": round(start_s, 3), "end_s": round(end_s, 3), "kind": kind, "mood": 1})
        for _ in range(bars * POINTS_PER_BAR):
            low.append(bass * 100)
            mid.append(energy * 60)
            high.append(energy * 40)
        if vocals:
            regions.append({"start_s": round(start_s, 2), "end_s": round(end_s, 2), "intensity": 80})
        bar += bars
    return {
        "stable_id": stable_id,
        "points": len(low),
        "waveform": {
            "kind": "tri",
            "preview": {"length": 0, "low": [], "mid": [], "high": []},
            "detail": {"length": len(low), "low": low, "mid": mid, "high": high},
        },
        "beatgrid": {"source": "rekordbox", "beat_count": len(beats), "beats": beats},
        "cues": [],
        "phrases": phrases,
        "phrases_source": "rekordbox",
        "vocals": (
            {"status": "rekordbox" if regions else "no_vocals", "fps": 150, "regions": regions}
            if vocals_analyzed
            else {"status": "not_analyzed"}
        ),
    }


def track_row(stable_id: str, *, bpm: float = 128.0, key: str = "8A", bars: int = 128, lead_in_s: float = 0.0) -> dict[str, Any]:
    return {
        "stable_id": stable_id,
        "title": f"Invented {stable_id}",
        "artist": "Fixture Artist",
        "bpm": bpm,
        "key": key,
        "duration_ms": int((lead_in_s + bars * 4 * 60.0 / bpm) * 1000),
        "stems_available": False,
    }


def profile(stable_id: str, *, bpm: float = 128.0, key: str = "8A", **kwargs: Any) -> TrackProfile:
    arrangement = kwargs.get("arrangement", DEFAULT_ARRANGEMENT)
    bars = sum(row[1] for row in arrangement)
    return profile_from_payloads(
        anlz_payload(stable_id, bpm=bpm, **kwargs),
        track_row(stable_id, bpm=bpm, key=key, bars=bars, lead_in_s=kwargs.get("lead_in_s", 0.0)),
    )
