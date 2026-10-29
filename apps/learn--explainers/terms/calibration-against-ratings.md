---
term: Calibration against ratings
slug: calibration-against-ratings
aliases: [quality signal calibration, proxy calibration, taste calibration, criterion validity]
group: Evaluation and method
level: 2
one_liner: Pick the measurable number that best agrees with a person's blind ratings, then lock it in as the bar.
why_it_matters_here: It is how a subjective "this blend feels smooth" becomes a number Open DJ can test and ratchet every round.
related: [bradley-terry, held-out-set, external-validation, veto-vs-weighted-score, loudness-continuity]
sources:
  - Thurstone, "A Law of Comparative Judgment", Psychological Review 34(4), 1927
  - ITU-R BS.1534, Method for the subjective assessment of intermediate quality level of audio (MUSHRA), 2015
  - Wikipedia, "Criterion validity" (secondary pointer)
status: draft
---

## In plain words

Much of what makes a transition "good" is taste. A computer cannot measure feel directly, so we
pick a measurable proxy, a number computed from the audio, and check it against a person's
blind ratings: they listen without knowing which variant is which, they rate or pick, and we see
which proxy agrees with them most. The winner goes into the spec as a pass mark and a regression
metric. Calibration is only for the taste layer. Hygiene, meaning objective correctness (no
clipping, no NaN, beats aligned within a few milliseconds, a null test that cancels), needs no
ratings: it is right or wrong. Spending a person's ears on hygiene wastes the scarcest resource
we have, so ratings go only to the judgements that are still uncertain, and the proxy is later
checked on ratings it was not fitted to.

## Picture

```text
 blind      proxy X, tracks the ear          proxy Y, does not
 rating
   5 |                       *                 *        *
   4 |                  *  *                       *       *
   3 |             *  *                        *  *    *
   2 |        *  *                               *   *
   1 |   *  *                                  *        *
     +-------------------------------    ----------------------------
       low      proxy value     high       low     proxy value    high

       agreement 0.86, keep X              agreement 0.12, drop Y
```

## In Open DJ

`(private path)` is the agreement ledger: for each judgement type the
project makes about audio, it counts how often the founder's ear confirmed or contradicted it and
derives a confidence of (confirmed + 1) / (confirmed + contradicted + 2), so a type with no
ratings reads exactly 0.500, "we do not know", and listening time goes there first.
`apps/lyrics/vocal_presence.py` chose its no-lyrics threshold of 12.5 percent coverage from blind
ratings on two corpora, set to lose none of the ear-verified vocal tracks. The transitions
research plans the same for transition taste, fitted by Bradley-Terry.
