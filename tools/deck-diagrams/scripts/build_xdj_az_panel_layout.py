from __future__ import annotations

_TRIM_CC = {1: 1, 2: 6, 3: 12, 4: 80}
_HI_CC = {1: 2, 2: 7, 3: 14, 4: 81}
_MID_CC = {1: 3, 2: 8, 3: 15, 4: 92}
_LOW_CC = {1: 4, 2: 9, 3: 21, 4: 82}
_COLOR_CC = {1: 5, 2: 10, 3: 22, 4: 83}
_FADER_CC = {1: 17, 2: 18, 3: 19, 4: 20}
_CH_CUE_CC = {1: 70, 2: 71, 3: 72, 4: 73}
_STRIP_ORDER = [3, 1, 2, 4]

# Row and column mates snapped to one line in panel.json (centre coordinate shared).
_ALIGN_ROWS: list[list[str]] = [
    [
        "loop-in",
        "loop-out",
        "reloop-exit",
        "deck-select-a",
        "deck-select-b",
        "call-prev",
        "call-next",
        "delete",
        "memory",
    ],
    ["4-beat", "8-beat", "vinyl-speed-adjust"],
    ["beat-jump-rev", "beat-jump-fwd"],
    ["beat-sync", "sync-master"],
    ["track-search-rev", "track-search-fwd"],
    ["search-rev", "search-fwd"],
    [
        "pad-mode-hot-cue",
        "pad-mode-beat-loop",
        "pad-mode-slip-loop",
        "pad-mode-beat-jump",
    ],
    ["pad-1", "pad-2", "pad-3", "pad-4"],
    ["pad-5", "pad-6", "pad-7", "pad-8"],
]
_ALIGN_COLS: list[list[str]] = [
    ["loop-in", "4-beat", "slip"],
    ["loop-out", "8-beat"],
    ["cue", "play-pause"],
    ["tempo-range", "master-tempo"],
    ["pad-mode-hot-cue", "pad-1", "pad-5"],
    ["pad-mode-beat-loop", "pad-2", "pad-6"],
    ["pad-mode-slip-loop", "pad-3", "pad-7"],
    ["pad-mode-beat-jump", "pad-4", "pad-8"],
    ["track-search-rev", "search-rev"],
    ["track-search-fwd", "search-fwd"],
]
_GLOBAL_ALIGN_ROWS: list[list[str]] = [
    ["back", "tag-track"],
    ["load-1", "load-2"],
    ["load-3", "load-4"],
    ["beat-left", "beat-right"],
    ["auto-tap", "tap", "fx-quantize"],
    ["freq-low", "freq-mid", "freq-hi"],
    ["ch-select-3", "ch-select-1", "ch-select-2", "ch-select-4"],
    ["ch-select-mic", "ch-select-cf-a", "ch-select-cf-b", "ch-select-master"],
    ["scfx-space", "scfx-dub-echo"],
    ["scfx-crush", "scfx-sweep"],
    ["scfx-noise", "scfx-filter"],
    ["master-eq-low", "master-eq-mid", "master-eq-hi", "booth-level"],
    ["mic1-eq-low", "mic1-eq-mid", "mic1-eq-hi"],
    ["mic2-eq-low", "mic2-eq-mid", "mic2-eq-hi"],
    [f"ch{c}-trim" for c in _STRIP_ORDER],
    [f"ch{c}-hi" for c in _STRIP_ORDER],
    [f"ch{c}-mid" for c in _STRIP_ORDER],
    [f"ch{c}-low" for c in _STRIP_ORDER],
    [f"ch{c}-color" for c in _STRIP_ORDER],
    [f"ch{c}-cue" for c in _STRIP_ORDER],
    [f"ch{c}-fader" for c in _STRIP_ORDER],
]
_GLOBAL_ALIGN_COLS: list[list[str]] = [
    ["fx-select", "fx-time", "level-depth", "fx-on-off", "tap"],
    ["phones-mix", "phones-level", "parameter"],
    ["scfx-space", "scfx-crush", "scfx-noise"],
    ["scfx-dub-echo", "scfx-sweep", "scfx-filter"],
    ["mic1-eq-low", "mic2-eq-low"],
    ["mic1-eq-mid", "mic2-eq-mid"],
    ["mic1-eq-hi", "mic2-eq-hi"],
    *[[f"ch{c}-{p}" for p in ("trim", "hi", "mid", "low", "color", "cue", "fader")] for c in _STRIP_ORDER],
]
