---
term: Chroma
slug: chroma
aliases: [chromagram, chroma vector, pitch-class profile, PCP, chroma features]
group: Harmony and analysis
level: 2
one_liner: "Twelve numbers per moment, one per note name C to B, saying how much of each note is sounding. Key detection reads it."
why_it_matters_here: "Open DJ's own key detector computes chroma for the whole track and matches it against 24 key templates."
related: [key-confidence, camelot-wheel, relative-and-parallel-keys, timbre-and-mfcc, key-clash]
sources:
  - "Mueller, Fundamentals of Music Processing, Springer, 2nd ed. 2021, chroma features"
  - "Krumhansl, Cognitive Foundations of Musical Pitch, Oxford University Press, 1990"
  - "librosa documentation, feature.chroma_cqt, https://librosa.org/doc/"
  - "Wikipedia, Chroma feature (secondary pointer)"
status: draft
---

## In plain words

Chroma throws away which octave a note is in and keeps only its name. A low A on the bassline,
a middle A on the pads and a high A in the vocal all pour into the same bucket. The result is
twelve buckets, C through B, measured again every few tens of milliseconds. Plot them over time
and you get a chromagram: a 12-row picture of the harmony with the drums mostly washed out.

Key detection works by averaging chroma across the track and asking which key's "shape" it
resembles. Each key has a template of how often its notes tend to appear (the home note and the
fifth strongest, notes outside the scale weakest); the standard ones come from Krumhansl's
listening experiments. Rotate the major and minor templates to all twelve home notes, correlate
each of the 24 with the track's profile, and the best correlation wins. The size of that win is
the key confidence.

## Picture

```text
   note   time >
   B      . . . # # # . . . . . .
   A#     . . . . . . . . . . . .
   A      # # # . . . # # # # # #    home note 9 of 12
   G#     . . . . . . . . . . . .
   G      . . . # # # . . . . . .
   F#     . . . . . . . . . . . .
   F      . . . . . . # # # . . .
   E      # # # . . . . . . # # #    fifth
   D#     . . . . . . . . . . . .
   D      . . . # # # . . . . . .
   C#     . . . . . . . . . . . .
   C      # # # . . . # # # # # #    minor third
          Am    G     F     Am       chords read off the columns

   average rows > match 24 templates > best is A minor = 8A
```

## In Open DJ

`apps/analysis/backends/own_key.py` decodes the full track, computes chroma with librosa's
`chroma_cqt`, and passes it to `estimate_key_krumhansl` in `apps/analysis_key/profiles.py`.
`apps/analysis_key/segments.py` repeats the estimate per segment so key changes can surface.
