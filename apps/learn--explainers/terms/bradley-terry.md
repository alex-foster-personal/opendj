---
term: Bradley-Terry model
slug: bradley-terry
aliases: [Bradley-Terry-Luce, BTL, paired comparison model, pairwise preference model]
group: Evaluation and method
level: 3
one_liner: Bradley-Terry turns many "A or B?" picks into one strength per item, so blind pairwise votes become a scale.
why_it_matters_here: It is the planned way to turn the founder's blind A/B picks between transitions into weights for the quality measure.
related: [calibration-against-ratings, held-out-set, veto-vs-weighted-score, noise-band-and-bootstrap]
sources:
  - Bradley and Terry, "Rank Analysis of Incomplete Block Designs I. The Method of Paired Comparisons", Biometrika 39(3/4), 1952
  - Hunter, "MM Algorithms for Generalized Bradley-Terry Models", Annals of Statistics 32(1), 2004
  - Agresti, Categorical Data Analysis, 3rd ed., Wiley, 2013 (chapter on paired comparisons)
status: draft
---

## In plain words

Ask a DJ to rate a transition out of 10 and the answer drifts with mood, with the previous
track, with what "7" means today. Ask "A or B?" and the answer is far more stable. The
Bradley-Terry model turns a pile of those pairwise picks into one number per item, a strength,
chosen so the predicted chance that A beats B matches the picks as closely as possible:

    P(A beats B) = 1 / (1 + exp(-(b_A - b_B)))

It suits blind A/B ratings for three reasons. A comparison needs no shared anchor. Not every
pair has to be heard, because strengths link through chains (A beat B, B beat C). And the fit
reports its own uncertainty. The useful extension: make each strength a weighted sum of measured
features of the transition, and fitting the picks yields the weights, which is how ratings
calibrate a measure.

## Picture

```text
 blind picks, 4 votes per pair            fitted strengths
 pair      votes          A won
 A vs B    A A A B        3 of 4          b_A   1.4   ##########
 B vs C    B B C B        3 of 4          b_B   0.3   ####
 A vs C    A A A A        4 of 4          b_C  -0.4   #
 C vs D    C D C C        3 of 4          b_D  -0.9
 B vs D    B B B D        3 of 4

           low                                       high
 scale     +----+----+----+----+----+----+----+----+----+
             D      C        B                  A

 predicted P(A beats C) = 1 / (1 + exp(-1.8)) = 0.86
 A vs D was never played, the chain still places them
```

## In Open DJ

Not yet in code. The transitions research (quality evaluation and the macro quality measure,
Tue 27 Oct 2026) plans it: the taste layer's weights start from hand-picked values, then are
fitted by Bradley-Terry in its logistic form on factor deltas between the two candidates of each
blind pair, while vetoes stay as hard gates outside the fit. It would sit beside the round log in
`apps/analysis_bench/rounds.py`, with the picks stored per round and the fit judged on held-out
pairs.
