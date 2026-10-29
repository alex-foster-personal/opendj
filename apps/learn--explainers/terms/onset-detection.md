---
term: Onset detection
slug: onset-detection
aliases: [onsets, transient detection, attack detection, spectral flux]
group: Harmony and analysis
level: 2
one_liner: "Finding the exact moments notes and drum hits start. Sharp onsets are the ruler for checking a beat grid or an alignment."
why_it_matters_here: "Open DJ stores onset times per track and uses them to propose hot cues and to spot a beat grid that starts in the wrong place."
related: [beat-grid, bar-phase, section-and-phrase-detection, timbre-and-mfcc, chroma]
sources:
  - "Bello et al., A Tutorial on Onset Detection in Music Signals, IEEE Transactions on Speech and Audio Processing, 2005"
  - "Mueller, Fundamentals of Music Processing, Springer, 2nd ed. 2021, onset detection"
  - "Wikipedia, Onset (audio) (secondary pointer)"
status: draft
---

## In plain words

An onset is the instant something starts: a kick lands, a hat ticks, a piano key goes down.
Detecting onsets means scanning the audio for sudden rises in energy. The common recipe is
spectral flux: cut the audio into short frames, compare each frame's spectrum to the one before,
add up only the increases, and pick the peaks of that curve. Drums give tall, narrow peaks; a
slow pad fading in gives almost none.

Onsets anchor alignment measurements because they are the sharpest events in a track. If you
want to know whether a beat grid sits on the kick or 12 milliseconds late, or whether two decks
are truly in phase, you compare grid lines to onset times. A sustained chord has no single
moment to measure against; a kick does. Onsets are also where a cue point feels right to a DJ,
because pressing play on one starts the track with a hit, not mid-note.

## Picture

```text
   audio       |#|....|#|..|.|..|#|....|#|..|.|..
                kick   kick hat  kick   kick hat

   flux        #      #   .  .  #      #   .  .
   curve       #      #   .  .  #      #   .  .
               #      #         #      #
               +---------------------------------> time
   onsets      *      *   *  *  *      *   *  *

   grid        |             |             |
               beat 1        beat 2        beat 3
               onset and grid agree within a few ms  =  grid is good
```

## In Open DJ

Analysis records carry `onsets_s`. `apps/analysis/detect_bad_beatgrid.py` compares the first
transient to the first downbeat to flag a grid that starts in the wrong place, and
`apps/analysis/auto_cues.py` proposes hot cues by picking the loudest onset in each time bin and
snapping it to a nearby downbeat.
