---
term: Phase correction
slug: phase-correction
aliases: [phase lock, sync drift correction, tempo trim, nudge]
group: Time, sync and loops
level: 2
one_liner: "How sync keeps two decks in step over time: ignore tiny errors, nudge the tempo for small ones, and seek when they are far apart."
why_it_matters_here: "Matching BPM once is not enough; grids and clocks drift, and this loop is what stops a synced mix from slowly flamming."
related: [beat-grid, bar-phase, cents, time-stretch, audio-clock-vs-page-tick]
sources:
  - "Astrom and Murray, Feedback Systems: An Introduction for Scientists and Engineers, Princeton University Press, 2008 (proportional control, deadband, saturation)"
  - "Pioneer DJ CDJ-3000 Operating Instructions (Beat Sync), AlphaTheta"
status: draft
---

## In plain words

Two decks set to the same BPM still wander apart: the grids are not perfect, tempos inside a
track drift, and measurements jitter. Phase correction is the small loop that keeps checking
"how far apart are the beats right now?" and steers. It works like a driver keeping a lane:
ignore a tiny wobble, steer gently for a small drift, and if you are way off, stop and re-place
the car instead of swerving.

The precise parts are a dead band (errors this small get no correction, so the loop does not
chase its own noise), a proportional nudge (a tempo trim sized to the error), a cap on that trim
(so it never becomes an audible pitch bend), and a seek (a jump) when the error is too big to
walk back in time. The trim is roughly: trim = error / correction window, then clamped to the
cap.

## Picture

```text
phase error, follower minus master (ms)

  -15        -2    0    +2                  +15
---+----------+----+----+-------------------+--------->
 SEEK | nudge  | dead band | nudge, capped   | SEEK
      |        |  no trim  |                 |

inside +-2 ms     play the base sync tempo exactly
2 to 15 ms        trim the tempo to remove the error over 4 beats,
                  never more than 0.3 % of the base tempo
past 15 ms or     lock lost: re-join with one seek, which is quicker
a quarter beat    than walking the error back for half a minute
```

## In Open DJ

This is `apps/webui/frontend/src/lib/rb/phase-lock.ts`. The dead band is 2 ms
(`PHASE_LOCK_DEADBAND_MS`); once trimming, it holds until the error is under 1 ms, so an error
sitting on the edge does not flip on and off. The nudge corrects over 4 master beats, the trim
cap is 0.3 percent (`PHASE_LOCK_MAX_TRIM` in `beat-sync-math.ts`, about 5 cents), and a re-join
seek happens past 15 ms or a quarter beat, confirmed over 3 ticks and no sooner than 8 seconds
after the last join. Uneven rekordbox dynamic grids are read through a smoothed copy so the lock
does not chase their 5 to 10 ms steps.
