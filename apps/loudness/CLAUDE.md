# apps/loudness - agent instructions

Offline EBU R128 loudness measurement for one audio file, via the ffmpeg
`ebur128` filter. This is the analysis-time half of level metering; the
realtime half lives in the frontend (`$lib/rb/meter-math`, `$lib/rb/meter-tap`).

## What it is for

Per-track gain. Measuring loudness offline and applying a per-track gain is what
rekordbox and Serato call auto-gain, and it removes most of the reason a channel
meter ever reaches the red: tracks arrive at the mixer already levelled.

    python -m apps.loudness scan TRACK [TRACK ...] [--target-lufs -14] [--json]

## Design constraints

- **ffmpeg is invoked as a subprocess, never linked.** An arm's-length CLI call
  is not linking, so this stays clean under the repo's Apache-2.0 licence
  regardless of how the local ffmpeg was built. Do not replace it with an
  in-process GPL library without reading the licensing section of
  `docs/research/adrian-level-meters-clipping-lights.md` first.
- **No fallback measurement path.** A missing ffmpeg, a missing filter, a
  non-zero exit and an unparseable summary are all `LoudnessError`. A loudness
  figure that silently came from somewhere else is worse than no figure.
- **No hidden defaults on the numbers that matter.** `--target-lufs` and
  `--ceiling-dbtp` are explicit; the module constants are the EBU broadcast
  target (-23) and the -1 dBTP mastering ceiling, not a guess about intent.
- **Gain is clamped by peak headroom** and reports `gain_clamped` when the
  clamp, rather than the loudness target, decided the number.

## Traps found the hard way

- `sine=` in ffmpeg's lavfi is **not full scale** (it measures -18.1 dBFS).
  Test fixtures use `aevalsrc=<amplitude>*sin(2*PI*1000*t)`, which is exact.
  A fixture built on `sine=` shifts every expected number by 18 dB.
- The `ebur128` filter prints running per-frame values as well as the final
  summary. The parser takes the LAST match per field for that reason; taking the
  first yields a mid-track sample presented as the integrated figure.
- Real masters routinely measure **above** 0 dBFS true peak. A track in the
  local library reads +2.3 dBTP. Do not assume true peak is negative.

## Tests

    .venv/bin/python -m pytest tests/loudness/ -v

They generate real audio with ffmpeg rather than mocking it: the entire purpose
of this module is agreeing with a real ffmpeg, so a mocked one tests nothing.
