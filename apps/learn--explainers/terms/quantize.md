---
term: Quantize
slug: quantize
aliases: [Q, snap to grid, quantized launch]
group: Time, sync and loops
level: 1
one_liner: "Quantize moves a cue, loop or jump you trigger a little early or late onto the nearest beat on the grid, so it still lands in time."
why_it_matters_here: "With quantize on, a hot cue or beat jump pressed slightly off still lands on the beat, as long as the grid itself is right."
related: [beat-grid, bar-phase, sample-accurate-scheduling, audio-clock-vs-page-tick]
sources:
  - "rekordbox Operating Instructions (Quantize), AlphaTheta / Pioneer DJ"
  - "Serato DJ Pro Software Manual (Quantize), Serato"
status: draft
---

## In plain words

Human hands are a few tens of milliseconds sloppy, and ears notice. Quantize lets the software
fix that: when you press a hot cue, set a loop or jump, it moves the action onto the beat grid.
There are two flavors. For a point you are setting (a loop in point, a cue), it snaps to the
nearest grid line. For an action that starts playback or jumps while playing, it waits for the
next beat (or bar) and fires exactly there.

The catch is that quantize can only be as good as the grid. On a badly gridded track it
confidently snaps everything to the wrong place, which is worse than no quantize at all.

## Picture

```text
grid        |---------------|---------------|---------------|
beat        1               2               3               4
                              * you press a hot cue here
                              |  (40 ms after beat 2)

Q off       plays at the press, 40 ms off the beat
                              *==============================>

Q on, set   the cue point snaps back to the nearest line, beat 2
                            #<

Q on, play  waits for the next line, then fires exactly on beat 3
                                            #================>
```

## In Open DJ

Each deck carries a quantize switch, and Settings has "Quantize on new decks"
(`deck_default_quantize` in `apps/webui/frontend/src/lib/settings/catalog.ts`).
`apps/webui/frontend/src/lib/player/transport/quantized-launch.ts` plans a quantized launch: it
picks the next beat on the grid and schedules the start at that moment on the audio clock, or
refuses with a reason if it cannot. Loop halving and doubling keep loop edges on whole grid
beats (`player/transport/loops.ts`). Analysis uses the same idea offline:
`apps/analysis_structure/quantize.py` snaps section labels to the grid.
