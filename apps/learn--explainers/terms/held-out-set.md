---
term: Held-out set
slug: held-out-set
aliases: [test set, holdout, train-test split, validation set, out-of-sample]
group: Evaluation and method
level: 2
one_liner: Tune a measure on one set of examples and judge it on another it never saw; grading on the tuning set flatters it.
why_it_matters_here: Any pass mark Open DJ fits to ratings or labels is quoted from a held-out set, so the number predicts new tracks, not the ones it memorized.
related: [auc, calibration-against-ratings, bradley-terry, noise-band-and-bootstrap]
sources:
  - Hastie, Tibshirani and Friedman, The Elements of Statistical Learning, 2nd ed., Springer, 2009 (chapter 7)
  - James, Witten, Hastie and Tibshirani, An Introduction to Statistical Learning, Springer, 2013 (chapter 5)
  - Wikipedia, "Training, validation, and test data sets" (secondary pointer)
status: draft
---

## In plain words

If you tune a measure on some examples and then grade it on the same ones, it looks great,
partly because it has learned their quirks rather than the general rule. That is overfitting.
So split the examples before you start. The fit set (training set) is where you choose weights
and thresholds, as often as you like. The held-out set (test set) is locked away during fitting
and scored once, at the end, and that score is the one you quote. Split by group, not by single
item: if one artist has tracks on both sides, the measure can learn that artist's sound and look
smarter than it is. And do not keep peeking. Tweaking until the held-out number looks good
quietly turns it into a second fit set; when that happens, collect a fresh one.

## Picture

```text
 all labeled examples, tagged by artist
 +--------------------------------------------------------------+
 | a1 a1 a2 a3 a3 a3 a4 a5 a5 a6 a7 a7 a8 a9 a9 a10 a10         |
 +--------------------------------------------------------------+
               split by ARTIST, never by single track
                  |                                |
                  v                                v
 +--------------------------------+   +---------------------------+
 | FIT SET, 60 percent            |   | HELD-OUT, 40 percent      |
 | a1 a2 a3 a5 a7 a9              |   | a4 a6 a8 a10              |
 | pick weights and thresholds    |   | locked away, scored       |
 | as often as you like           |   | ONCE, at the end          |
 +--------------------------------+   +---------------------------+
        fit score 0.99                    held-out score 0.97
        flattering, it saw these          the number you quote
```

## In Open DJ

`apps/lyrics/vocal_presence.py` was calibrated on 373 MTG-Jamendo tracks split 60/40 by artist,
and reports its held-out AUC (0.970) and held-out precision and recall rather than the fit-set
numbers. The playlist-ordering research asks for a held-out AUC of at least 0.75 on a 30 percent
split, and the macro quality measure research requires the taste weights fitted on round 0's
ratings to agree with held-out ratings at least 75 percent of the time, and the automatic
chooser to match the founder's picks on held-out pairs before it runs unattended.
