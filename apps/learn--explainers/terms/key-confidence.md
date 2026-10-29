---
term: Key confidence
slug: key-confidence
aliases: [key strength, key certainty, tonal clarity, margin, no tonal center]
group: Harmony and analysis
level: 2
one_liner: "How sure a key detector is about its answer. A low-confidence key is a guess and should not filter or rank your tracks."
why_it_matters_here: "Open DJ flags a low-confidence key as no_tonal_center and leaves it inert instead of letting a guess pass as a finding."
related: [chroma, camelot-wheel, relative-and-parallel-keys, key-clash, calibration-against-ratings]
sources:
  - "Krumhansl, Cognitive Foundations of Musical Pitch, Oxford University Press, 1990"
  - "Mueller, Fundamentals of Music Processing, Springer, 2nd ed. 2021, tonal analysis chapter"
  - "Mixed In Key, Harmonic Mixing book, https://mixedinkey.com/Book/"
status: draft
---

## In plain words

A key detector never says "I don't know" on its own. It compares the track's notes against all
24 keys and always returns the best match, even for a drum loop with no tune. Confidence is how
well that winner actually matched. A second number matters just as much: the margin, how far the
winner beat the runner-up. If the top two are nearly tied, the detector flipped a coin.

Low confidence happens on drones, noise intros, percussion-only tools, tracks that change key,
and modal music that does not fit major or minor templates. The runner-up is usually the relative
key or a fifth away, which is why detectors disagree with each other in those directions.

A low-confidence key should not drive a decision because a wrong key fails twice: it hides good
tracks from the suggester and recommends ones that clash. Showing the key with a warning is
honest; ranking by it is not.

## Picture

```text
   clear track                         ambiguous track
   Am  ##########  .71                 Am  ######  .42
   C   #######     .55                 C   ######  .41
   Em  #####       .40                 Em  #####   .39
   ...                                 ...
   margin .16 > publish 8A             margin .01 > flag no tonal center
```

## In Open DJ

`apps/analysis_key/flags.py` raises `no_tonal_center` when either the winning correlation or the
margin is below threshold, and the result goes inert with a reason rather than `status: ok`. The
margin threshold of 0.005 was measured on Fri 2 Oct 2026 against 60 tagged tracks: it keeps about
two thirds of them and lifts agreement with the tag key from 60 to 75 percent. The confidence
threshold is still an uncalibrated placeholder. Records carry `key_confidence` beside the key.
