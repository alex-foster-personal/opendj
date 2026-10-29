---
term: Energy boost mix
slug: energy-boost-mix
aliases: [plus two, +2 mix, whole-tone lift, energy drop, key bump]
group: Harmony and analysis
level: 2
one_liner: "Jump two numbers up the Camelot wheel, a whole tone higher, to lift the room. Best as a quick, phrase-aligned cut, not a long blend."
why_it_matters_here: "Open DJ has energy boost and energy drop as named key moves, off by default because they are a choice, not a safe match."
related: [camelot-wheel, semitone-and-pitch-shift, fifths-and-fourths, section-and-phrase-detection, key-clash, bar-phase]
sources:
  - "Mixed In Key, Use Advanced Harmonic Mixing Techniques, https://mixedinkey.com/Book/Use-Advanced-Harmonic-Mixing-Techniques/"
  - "DJ.Studio, How To DJ using compatible Keys, https://dj.studio/blog/compatible-keys"
  - "Wikipedia, Modulation (music) (secondary pointer)"
status: draft
---

## In plain words

An energy boost mix moves two numbers clockwise on the same letter, 8A to 10A or 8B to 10B.
Because each wheel step is a fifth, two steps land exactly one whole tone (two semitones) above
where you started: C major becomes D major. Everything the crowd was humming suddenly sits
higher, and the ear reads that as a lift, the same trick pop songs use in their last chorus.

The catch is that the two keys share only five of seven notes. Overlapping them for a long
blend lets the two odd notes rub. So DJs use it as a short move: mix the drums of the incoming
track early, keep its melody out with EQ, then cut cleanly on a phrase start (a drop or a new
16-bar section) so the key change arrives as an event rather than a smear. The reverse move, two
numbers counterclockwise, is an energy drop and works the same way to cool a floor down.

## Picture

```text
              C  C# D  D# E  F  F# G  G# A  A# B
C major 8B    *  .  *  .  *  *  .  *  .  *  .  *
D major 10B   .  *  *  .  *  .  *  *  .  *  .  *    5 of 7 notes shared

   pitch                       incoming 10B
     |                       +=============
     |   outgoing 8B         |   one whole tone up
     +=======================+
     |                       |
     +-------------------------------------> bars
                             16
                             cut on the phrase start
```

## In Open DJ

`CAMELOT_RULES` in `apps/shared/harmonic.py` defines `energy_boost` (+2 on the same letter) and
`energy_drop` (-2), both off by default, citing the Mixed In Key and DJ.Studio pages above.
`camelot_compatibility` scores a two-step same-letter pair 0.5, against 0.9 for one step, so the
suggester will not push a boost unless the DJ turns the rule on.
