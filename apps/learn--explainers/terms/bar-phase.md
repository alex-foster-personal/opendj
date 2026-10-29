---
term: Bar phase
slug: bar-phase
aliases: [beat number, beat-in-bar, downbeat alignment, phrase alignment]
group: Time, sync and loops
level: 2
one_liner: "Which beat of the bar a moment sits on, 1 to 4; two decks can share every beat and still disagree about which one is the 1."
why_it_matters_here: "Beat sync in BAR mode refuses to lock two decks unless their bar phases agree, so drops and phrase changes land together."
related: [beat-grid, phase-correction, quantize, half-double-tempo]
sources:
  - "Pioneer DJ CDJ-3000 Operating Instructions (Beat Sync, bar and phrase display), AlphaTheta"
  - "Butler, Unlocking the Groove: Rhythm, Meter, and Musical Design in Electronic Dance Music, Indiana University Press, 2006"
status: draft
---

## In plain words

Dance music counts in fours, and the "1" matters: that is where the drop hits, where the
bassline restarts, where a new 8, 16 or 32 bar phrase begins. Bar phase is simply "which count
are we on right now". Two decks can be perfectly beatmatched, every kick landing together, while
one deck is on its 1 and the other is on its 4. It sounds fine for a while, then the second
track's drop arrives a beat early and the whole mix feels like it tripped.

Precisely: bar phase is the beat index modulo the bar length (4 here), counted from the nearest
preceding downbeat. Beat phase is the fraction of the way through the current beat. Sync needs
both.

## Picture

```text
Deck A, master    1   2   3   4   1   2   3   4   1  <- A's drop
                  #   .   .   .   #   .   .   .   #

Deck B, right     1   2   3   4   1   2   3   4   1  <- B's drop
                  #   .   .   .   #   .   .   .   #
                  kicks line up AND bars line up

Deck B, wrong     2   3   4   1   2   3   4   1   2
                  .   .   .   #   .   .   .   #   .
                  kicks still line up, so beat sync looks happy,
                  but B's 1 falls on A's 4: every phrase change,
                  fill and drop on B lands one beat early
```

## In Open DJ

`apps/analysis_beatgrid/bar_phase.py` turns beat and downbeat times into numbers 1 to 4,
counting from the preceding downbeat, and keeps counts of bars that came out too long or too
short so a missed downbeat is reported instead of hidden. On the deck, Beat Sync defaults to BAR
mode (`apps/webui/frontend/src/lib/rb/beat-sync-math.ts`, described in
`docs/architecture/audio-timing.md`): an exact lock must land on the master's beat number. BEAT
mode drops that rule. A half or double time lock is shown in orange, because beat phase holds
while the bar count does not.
