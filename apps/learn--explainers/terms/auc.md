---
term: AUC (area under the ROC curve)
slug: auc
aliases: [ROC AUC, area under the curve, AUROC, concordance]
group: Evaluation and method
level: 2
one_liner: AUC is the chance a score ranks a random good example above a random bad one; 1.0 is perfect, 0.5 is a coin flip.
why_it_matters_here: It is how we check a quality measure separates real transitions from broken ones before anyone picks a pass mark.
related: [held-out-set, noise-band-and-bootstrap, external-validation, calibration-against-ratings]
sources:
  - Fawcett, "An Introduction to ROC Analysis", Pattern Recognition Letters 27(8), 2006
  - Hanley and McNeil, "The Meaning and Use of the Area under a ROC Curve", Radiology 143(1), 1982
  - Wikipedia, "Receiver operating characteristic" (secondary pointer)
status: draft
---

## In plain words

Take a pile of examples you already know are good or bad, and let a measure score each one.
Pick one good and one bad at random: AUC is the chance that the good one scored higher. At 1.0
the measure always ranks good above bad; at 0.5 it knows nothing, a coin flip; below 0.5 it is
upside down. The precise name is the area under the ROC (receiver operating characteristic)
curve, which plots the true-positive rate against the false-positive rate as you slide a cut-off
from strict to lenient. Its strength is that it needs no cut-off at all: it judges the ranking
first. Its limit is the flip side. It does not tell you where to put the pass mark, or how much
a false alarm costs compared with a miss; those are separate decisions made after the ranking is
shown to be good.

## Picture

```text
 measure A, AUC about 0.93: good and bad barely overlap

 score   0.0       0.25      0.5       0.75      1.0
         +---------+---------+---------+---------+
 bad     | b bb b  b bb  b  b   b                 |
 good    |                  g  g  g gg g  gg g g  |
         +---------+---------+---------+---------+

 measure B, AUC about 0.52: same spread, fully mixed

 bad     | b   b  b  b   b   b  b   b  b   b  b   |
 good    |  g   g  g   g   g  g  g   g   g   g    |
         +---------+---------+---------+---------+

 A ranks a random good above a random bad 93 times in 100
 B does it about half the time, no better than guessing
```

## In Open DJ

`apps/lyrics/vocal_presence.py` reports a held-out AUC of 0.970 for vocal coverage as an
instrumental score, measured on labeled MTG-Jamendo tracks, before its pass mark was chosen
from the founder's ratings. The transitions research (macro quality measure, Tue 27 Oct 2026)
sets AUC targets for the transition measure: 0.90 for the vetoes plus the core layer at telling
real DJ transitions from the same transitions shifted by a beat or a bar, and 0.75 for the taste
layer alone, each with a bootstrapped confidence interval. The playlist-ordering research asks
for a held-out AUC of at least 0.75 on a 30 percent split before a proxy is trusted.
