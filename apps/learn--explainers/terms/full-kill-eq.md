---
term: Full-kill EQ
slug: full-kill-eq
aliases: [full kill, kill EQ, EQ kill, kill switch, total kill, deeper shelf]
group: Filters and EQ
level: 1
one_liner: An EQ whose band, turned fully down, removes that band completely; it needs an isolator, because a shelf only ever turns a band down by a fixed amount.
why_it_matters_here: Open DJ's LOW knob at zero still leaks bass, so a two-bassline swap is less clean than on a full-kill mixer.
related: [isolator-eq, shelf-eq, crossover-and-bands, bell-eq, taper-law, linkwitz-riley-filter]
sources:
  - Allen and Heath, "Xone:92 User Guide" (EQ total kill), https://www.allen-heath.com/
  - Pioneer DJ, "DJM-900NXS2 Operating Instructions" (EQ curve ISOLATOR and EQ settings), https://www.pioneerdj.com/
  - Robert Bristow-Johnson, "Audio EQ Cookbook" (shelf gain and slope), W3C note, https://www.w3.org/TR/audio-eq-cookbook/
status: draft
---

## In plain words

On a full-kill mixer, turning the bass knob all the way down makes the bass disappear: the band
goes to silence, minus infinity in dB. Many mixers also have kill buttons that do this in one
press. DJs lean on it for the bass swap, where one track's bass is removed exactly as the other's
comes in, so two basslines never fight.

A shelving EQ cannot do this, for two reasons. Its depth is a fixed gain, often -26 dB, so the
bass is much quieter but still present. And its ramp from cut to flat has a fixed gentle slope:
making the cut deeper does not make the ramp steeper, it only drags the ramp further into the
mids, so at the corner frequency you get roughly half the cut and the low mids suffer as well.

So a kill is "not a deeper shelf". A shelf lowers everything below its corner by a fixed amount
and stops there; Open DJ's low shelf bottoms out at -26 dB. Pushing that shelf to -60 dB would
still be a shelf. A kill is a different filter, an isolator, which keeps attenuating toward zero
below the crossover, so nothing is left to leak.

A full kill needs an isolator: crossover filters split the bass off as its own band, and that band
is turned down to zero on its own. Some DJ software combines both, keeping shelves for the upper
part of the knob and fading an isolator band to zero at the bottom.

## Picture

```text
 BASS KNOB FULLY DOWN

 shelf EQ at -26 dB                 isolator ("full kill")
 dB                                 dB
    0 |            *******             0 |             *******
      |          **                      |            *
  -13 |        **  <- corner             |           *
  -26 |********                          |          *
      |  bass still there,               |         *
      |  26 dB quieter              -inf |*********   bass gone
      +-------+-------+--> Hz            +-------+-------+--> Hz
             100     250                        100     250
```

## In Open DJ

Not a full kill. The LOW knob is a 250 Hz shelf with a -26 dB floor (`EQ_MIN_DB` in
`apps/audio-engine/src/mixer.rs`), and the transitions research measured only about 13 dB of cut
at 250 Hz with the knob at zero. A true kill would come from an isolator EQ mode in the deck strip
(`apps/audio-engine/src/deck.rs`).
