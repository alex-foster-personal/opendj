---
term: Section and phrase detection
slug: section-and-phrase-detection
aliases: [structure analysis, segmentation, phrase detection, intro outro detection, drop detection]
group: Harmony and analysis
level: 2
one_liner: "Finding where the intro, build, drop, breakdown and outro start, and snapping them to phrases of 8, 16 or 32 bars."
why_it_matters_here: "Open DJ snaps a model's section boundaries onto the track's own downbeats and phrase grid, and records every snap it makes."
related: [beat-grid, bar-phase, onset-detection, energy-boost-mix, vocal-activity, source-separation-and-stems]
sources:
  - "Kim and Won, All-In-One Metrical and Functional Structure Analysis with Neighborhood Attentions on Demixed Audio, WASPAA 2023, arXiv:2307.16425"
  - "Mixed In Key, Harmonic Mixing book, phrasing chapters, https://mixedinkey.com/Book/"
  - "Mueller, Fundamentals of Music Processing, Springer, 2nd ed. 2021, music structure analysis"
status: draft
---

## In plain words

Dance music is built from blocks. Four beats make a bar, and bars come in phrases of 8, 16 or 32,
with something changing at each phrase boundary: a new element enters, the bass drops out, the
drop hits. Sections are the named blocks a DJ thinks in: intro, build, drop, breakdown, outro.
Mixing on phrase boundaries is what makes a transition feel planned rather than random, because
both tracks change at the same moment.

Section detection finds those boundaries automatically. A model listens for where the sound
changes character (energy, texture, repetition) and proposes boundary times and labels. Models
think in seconds, though, and a DJ thinks in bars, so the useful second step is to snap each
boundary onto the beat grid's downbeats and then onto a phrase multiple. A boundary that would
need a big move to snap is better left alone and flagged, because dragging it invents a phrase
the music does not have. Intros that are not a whole number of phrases (a 2-bar pickup) are why
the phrase count cannot just start at bar 1.

## Picture

```text
   bars   0       16      32      48      64      80      96      112
          |-------|-------|-------|-------|-------|-------|-------|
   label  intro           build   drop            break   outro
   energy ================--------################........========
   model         *        *      *                *      *
   snapped        *       *       *               *       *
          model lands up to a bar off  >  snapped to the phrase grid

   * = boundary   = calm   - = build   # = drop   . = breakdown
```

## In Open DJ

`apps/analysis_structure/allin1_runner.py` runs the All-In-One model for boundaries and labels;
its beats are discarded in favor of Open DJ's own beat grid. `apps/analysis_structure/quantize.py`
snaps each boundary to a downbeat, then to a phrase multiple, keeps the raw time and the distance
moved, leaves a boundary `unsnapped` when it would move more than a bar, and votes the phrase
offset rather than assuming bar 1. `drop_events.py` and `dj_labels.py` turn sections into DJ terms.
