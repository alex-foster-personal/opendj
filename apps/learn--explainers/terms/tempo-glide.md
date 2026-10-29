---
term: Tempo glide
slug: tempo-glide
aliases: [tempo ramp, BPM ramp, tempo blend, pitch ride]
group: Time, sync and loops
level: 2
one_liner: "Moving the tempo smoothly from one track's BPM to the next over several bars of a mix, instead of jumping in one step."
why_it_matters_here: "An automatic transition between tracks at different tempos needs a glide, or the crowd hears a lurch at the handoff."
related: [phase-correction, time-stretch, key-lock, equal-power-crossfade, gain-ramp]
sources:
  - "Broughton and Brewster, How to DJ Right: The Art and Science of Playing Records, Grove Press, 2003 (riding the pitch through a mix)"
  - "Pioneer DJ CDJ-3000 Operating Instructions (Tempo range, Beat Sync), AlphaTheta"
status: draft
---

## In plain words

Mixing a 128 BPM track into a 124 BPM one, a DJ has two choices. Jump: snap the tempo to 124 the
moment the new track takes over, a sudden 3 percent drop the room feels as a stumble. Glide:
while both tracks play, ride the pitch fader slowly, a fraction of a percent per bar, so by the
end of the blend everything sits at 124 and nobody noticed. Good DJs glide by hand; software can
do it exactly.

Precisely, a glide is a tempo curve over the transition, usually linear in BPM or in log tempo
over a whole number of bars, with both decks kept phase locked the whole way. With key lock off,
the pitch slides too (about 17 cents per percent), which is part of why glides stay small.

## Picture

```text
BPM
128 |=====
    |     =====
127 |          =====
    |               =====
126 |                    =====
    |                         =====
125 |                              =====
    |                                   =====
124 |                                        ===============
    +----+----+----+----+----+----+----+----+----+----+--> bars
    0    2    4    6    8   10   12   14   16   18   20
    ^ new track in, both playing            ^ old track out

jump : 128 -> 124 in one step at bar 16, a 3.1 % lurch
glide: 128 -> 124 over 16 bars, about 0.2 % per bar, unheard
```

## In Open DJ

Not yet as a track-to-track feature. The building blocks are there: when a synced master's
tempo changes, followers re-anchor through a short ramp (measured near 850 ms, recorded in
`(private path)` on Thu 1 Oct 2026), and the Rust engine has a linear
parameter glide over a fixed number of frames (`apps/audio-engine/src/dsp.rs`). A tempo glide
across a mix would belong in the auto-play handoff (`apps/webui/frontend/src/lib/rb/auto-play-handoff.ts`)
(a tempo glide is not yet in Open DJ).
