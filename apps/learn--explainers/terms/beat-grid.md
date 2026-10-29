---
term: Beat grid
slug: beat-grid
aliases: [grid, beatgrid, PQTZ grid]
group: Time, sync and loops
level: 1
one_liner: "A list of timestamps saying where every beat of a track falls, which beats start a bar, and the tempo between them."
why_it_matters_here: "Sync, quantize, beat loops, beat jumps and bar-aligned mixes all read the grid, so a wrong grid breaks every one of them at once."
related: [bar-phase, quantize, phase-correction, onset-detection, half-double-tempo]
sources:
  - "rekordbox Operating Instructions (beat grid editing), AlphaTheta / Pioneer DJ"
  - "Foscarin, Schluter, Widmer, Beat This! Accurate Beat Tracking Without DBN Postprocessing, ISMIR 2024"
  - "Beatmatching, Wikipedia (secondary pointer)"
status: draft
---

## In plain words

A beat grid is the ruler laid over a track. Each mark says "a beat happens here", and every
fourth mark is a downbeat, the first beat of a bar. Software reads the gaps between marks to get
the tempo, so a track that speeds up gets marks that slowly creep closer together. With a good
grid the computer knows the music's time as well as you do; with a bad one it confidently lines
things up in the wrong place.

The precise version: a grid file is an ordered list of beats, each holding a time in seconds (or
milliseconds), a beat number 1 to 4 inside its bar, and the local tempo. A fixed-tempo track can
be stored as one anchor plus a BPM; a "dynamic" grid stores every beat.

## Picture

```text
            bar 1                           bar 2
            |-------------------------------|---------------
time (s)    0.000   0.469   0.938   1.406   1.875   2.344
beat n      1       2       3       4       1       2
            #       .       .       .       #       .
            ^ downbeat                      ^ downbeat
            |<----->| one beat = 60 / 128 BPM = 0.469 s
            |<----------------------------->| one bar = 4 beats

one row of the grid file:   t = 1.875 s   n = 1   bpm = 128.00
```

## In Open DJ

Two sources feed the deck. The rekordbox grid comes from the track's ANLZ file (the PQTZ tag,
read with pyrekordbox) and reaches the page as the beat list in
`apps/webui/frontend/src/lib/rb/anlz-types.ts`. Our own grid is built in `apps/analysis_beatgrid`:
`beat_this_runner.py` finds beats and downbeats, `grid_fit.py` and `tempo_map.py` fit them into
a clean grid, and `grid_quality.py` flags grids that look suspect so sync can treat them with
care. A DJ sees the grid as the tick marks on the waveform.
