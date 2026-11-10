"""``scan`` subcommand and output helpers (split from ``apps.vocals.cli``).

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 scan: coverage report pvdi / cached-demucs / missing-file /
    missing-analysis / stem_derivable / todo,
    with todo ETA at the SPIKE-B2 1.36x-realtime CPU rate; --playlist
    filter; --json machine-readable (agent-native parity).
    [if] a track has PVDI in its .2EX [then] it counts as pvdi
    [if] an audio track has no local ANLZ .DAT [then ⛔️] it enters the
      trickle queue (Demucs output would be unservable through /anlz)
    [if] counts do not sum to total [then ⛔️]
    [if] --playlist names an unknown playlist [then ⛔️] explicit error

-Claude
"""

from __future__ import annotations

import argparse
import json

from apps.vocals.cli_library import (
    CATEGORY_BROKEN_CACHE,
    CATEGORY_CACHED,
    CATEGORY_MISSING,
    CATEGORY_MISSING_ANALYSIS,
    CATEGORY_PVDI,
    CATEGORY_STEM_DERIVABLE,
    CATEGORY_TODO,
    DEMUCS_REALTIME_FACTOR,
    Ctx,
    VocalTrack,
    classify,
    load_tracks,
)

# ----- output helpers ------------------------------------------------------------------


def _fmt_dur(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    return f"{seconds:.0f}s"


def _counts(tracks: list[VocalTrack]) -> dict[str, int]:
    counts = {
        CATEGORY_PVDI: 0,
        CATEGORY_CACHED: 0,
        CATEGORY_MISSING: 0,
        CATEGORY_MISSING_ANALYSIS: 0,
        CATEGORY_TODO: 0,
        CATEGORY_STEM_DERIVABLE: 0,
        CATEGORY_BROKEN_CACHE: 0,
    }
    for tr in tracks:
        counts[tr.category] += 1
    return counts


# ----- subcommands ----------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace) -> int:
    ctx = Ctx(data_dir=args.data_dir)
    tracks = load_tracks(ctx, args.playlist)
    classify(ctx, tracks)
    counts = _counts(tracks)
    if sum(counts.values()) != len(tracks):
        raise AssertionError("category counts do not sum to total - bug")
    todo_audio_s = sum(t.length_s for t in tracks if t.category == CATEGORY_TODO)
    eta_s = todo_audio_s * DEMUCS_REALTIME_FACTOR

    if args.json:
        print(
            json.dumps(
                {
                    "data_dir": str(ctx.data_dir),
                    "playlist": args.playlist,
                    "total": len(tracks),
                    **counts,
                    "todo_audio_s": todo_audio_s,
                    "todo_eta_s": round(eta_s, 1),
                    "realtime_factor": DEMUCS_REALTIME_FACTOR,
                },
                indent=1,
            )
        )
        return 0

    scope = f" playlist={args.playlist!r}" if args.playlist else ""
    print(f"vocal coverage scan{scope} (data-dir {ctx.data_dir})")
    print(f"  pvdi (rekordbox):  {counts[CATEGORY_PVDI]:>6}")
    print(f"  cached-demucs:     {counts[CATEGORY_CACHED]:>6}")
    print(f"  missing-file:      {counts[CATEGORY_MISSING]:>6}")
    print(f"  missing-analysis:  {counts[CATEGORY_MISSING_ANALYSIS]:>6}")
    print(f"  broken-cache:      {counts[CATEGORY_BROKEN_CACHE]:>6}")
    print(f"  stem-derivable:    {counts[CATEGORY_STEM_DERIVABLE]:>6}")
    print(
        f"  todo:              {counts[CATEGORY_TODO]:>6}"
        f"  ({_fmt_dur(todo_audio_s)} audio, "
        f"ETA {_fmt_dur(eta_s)} at {DEMUCS_REALTIME_FACTOR}x realtime)"
    )
    if counts[CATEGORY_STEM_DERIVABLE]:
        print("  (stem-derivable tracks: use `python -m apps.vocals from-stems --live`, not demucs trickle)")
    print(f"  total:             {len(tracks):>6}")
    return 0
