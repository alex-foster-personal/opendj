---
term: Loop roll
slug: loop-roll
aliases: [roll, beat roll, slip roll, momentary loop, stutter]
group: Time, sync and loops
level: 1
one_liner: "A beat loop that only lasts while you hold the pad, with slip on, so the track picks up on time the moment you let go."
why_it_matters_here: "Rolls are the classic build into a drop; done right they need beat loops, halving and slip working together on the grid."
related: [slip-mode, loop-seam, quantize, beat-grid, bar-phase]
sources:
  - "Serato DJ Pro Software Manual (Roll pad mode), Serato"
  - "Pioneer DJ CDJ-3000 Operating Instructions (Beat Loop, Slip Loop), AlphaTheta"
status: draft
---

## In plain words

Hold a roll pad and the deck repeats a short slice, 1 beat or 1/2 or 1/8, starting on the grid.
Let go and the track carries on as if nothing happened, because slip kept the song running
underneath. The classic use is a build: roll 1 beat, then 1/2, 1/4, 1/8, each halving faster
and more urgent, then release right on the drop, or stop a beat early and let one beat of silence
set the drop up. Because of slip, the drop arrives exactly where it was always going to.

Precisely: a roll is a momentary, quantized beat loop with slip engaged, and loop halving shrinks
the loop around its start while the hidden playhead keeps advancing.

## Picture

```text
beat      1       2       3       4       1 (drop)
          |       |       |       |       |
roll      AAAAAAAAaaaaaaaaBBBBbbbbCCccDdDd|
size      1 beat  1 beat  1/2 1/2 1/4 1/8 | each letter run = one repeat
hidden    ================================>  the song keeps running
                                          ^ release here: the hidden
                                            head is on the drop, so
                                            the drop lands in time

roll into a cut: stop the roll a beat early and let silence carry it
roll      AAAAAAAAaaaaaaaaBBBBbbbb        |#############
                                  silence  drop on the 1
```

## In Open DJ

Not yet: the pad mode "Beat Loop / Roll" is listed as not built in
`apps/webui/frontend/src/lib/rb/pad-mode-catalog.ts`, so the control renders inert with its
explainer. The pieces it needs exist: slip with a hidden playhead
(`apps/webui/frontend/src/lib/player/transport/slip-anchor.ts`), beat loops with halve and double
(`player/transport/loops.ts`), and beat loops in the Rust engine (`loop_beats` in
`apps/audio-engine/src/engine.rs`). A roll would be a press-and-hold wrapper over those.
