---
term: Key clash
slug: key-clash
aliases: [harmonic clash, dissonance, clashing keys, train wreck, sour mix]
group: Harmony and analysis
level: 2
one_liner: "The sour rub when two playing tracks' notes disagree. Real, but usually third in line after the beat and the low end."
why_it_matters_here: "Key is the heaviest weight in Open DJ's track match score, but nothing yet listens to the actual overlap for dissonance."
related: [camelot-wheel, fifths-and-fourths, semitone-and-pitch-shift, vocal-activity, key-confidence, bass-swap, isolator-eq, beat-grid]
sources:
  - "Mixed In Key, Harmonic Mixing book, https://mixedinkey.com/Book/"
  - "Plomp and Levelt, Tonal consonance and critical bandwidth, JASA, 1965"
  - "Wikipedia, Consonance and dissonance (secondary pointer)"
status: draft
---

## In plain words

A key clash is what you hear when two tracks play notes that do not belong together: a sour,
wobbly rub, worst on long held chords and basslines. Physically, two close but unequal pitches
beat against each other inside the ear's hearing bands, and that roughness reads as tension.
The worst pairs are a semitone or a tritone apart, which on the Camelot wheel are five and six
steps away.

When is it third-order, a smaller problem than it sounds? Most transitions fail first on the
beat (drums out of phase) and second on the low end (two kicks or basslines stacked, too loud).
Key matters after those, and it can almost vanish when the overlap is a few bars, when one track
is mostly drums, when the DJ has EQ'd the melodic parts of one track out, or when the detector's
key was a low-confidence guess anyway. It matters most for long, melodic blends and for two
vocals.

## Picture

```text
   wheel steps   interval          notes shared   sounds
   0             same key          7 of 7         clean
   1             fifth             6 of 7         clean
   2             whole tone        5 of 7         lift if short
   3             minor third       4 of 7         mood change
   4             major third       3 of 7         tense
   5             semitone          2 of 7         sour
   6             tritone           2 of 7         sour

   order of failure in a blend
   1  beat phase     2  low end and level     3  key
```

## In Open DJ

`track_compatibility` in `apps/shared/harmonic.py` weights key at 0.50, the largest share,
"since harmonic clashes are the most noticeable", and `camelot_compatibility` scores wheel
distance. Measuring real dissonance in the playing overlap, for example comparing both decks'
chroma during the blend window: not yet; it would belong in `apps/dj_copilot`.
