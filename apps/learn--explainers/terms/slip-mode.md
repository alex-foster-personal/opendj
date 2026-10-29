---
term: Slip mode
slug: slip-mode
aliases: [slip, SLIP, censor slip, slip loop]
group: Time, sync and loops
level: 1
one_liner: "With slip on, the track keeps playing silently underneath a loop, scratch or reverse, so letting go drops you back exactly where it would have been."
why_it_matters_here: "Slip lets a DJ loop, scratch or roll on a synced deck without losing the phrase, which is what makes rolls usable mid-mix."
related: [loop-roll, loop-seam, bar-phase, quantize, phase-correction]
sources:
  - "Pioneer DJ CDJ-3000 Operating Instructions (Slip mode), AlphaTheta"
  - "Serato DJ Pro Software Manual (Slip mode), Serato"
status: draft
---

## In plain words

Normally, if you loop one beat four times and let go, the track carries on from the end of the
loop. The song is now four beats behind where it was, so it no longer lines up with the other
deck's phrases. Slip mode fixes that by keeping a hidden playhead running in the background at
normal speed. You hear the loop (or the scratch, the reverse, the brake), but the song carries on
underneath, unheard. When you release, playback jumps to the hidden playhead and you land back
in the song exactly in time, as if the trick had been played over the top.

Precisely: slip keeps a second, linear transport position that advances with the deck's tempo
whatever the audible playhead does, and on release the audible playhead seeks to it.

## Picture

```text
              bar 1           bar 2           bar 3
beat          1   2   3   4   1   2   3   4   1
hidden head   ====================================>  never stops
audible       ====[2 ][2 ][2 ][2 ]============>
                  ^ loop on       ^ release

SLIP ON : resumes at the hidden head, bar 2 beat 2, still in phrase
SLIP OFF: resumes at bar 1 beat 3, right after the loop, so the song
          now runs one bar behind the phrase it was in
```

## In Open DJ

Each deck carries `slip_enabled` (armed), `slip_active` (a loop or scratch is holding it) and
`slip_position_ms`, the hidden linear playhead, in
`apps/webui/frontend/src/lib/rb/deck-state-types.ts`. The math for that hidden playhead,
including tempo changes made while slipping, lives in
`apps/webui/frontend/src/lib/player/transport/slip-anchor.ts`, kept free of any audio code so it
can be tested on its own.
