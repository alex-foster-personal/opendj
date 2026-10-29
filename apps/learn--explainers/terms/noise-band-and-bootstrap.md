---
term: Noise band and bootstrap
slug: noise-band-and-bootstrap
aliases: [noise floor, run-to-run variance, confidence interval, bootstrap resampling, minimum detectable effect]
group: Evaluation and method
level: 2
one_liner: The noise band is how far a number wobbles when nothing changed; the bootstrap measures that wobble by resampling your own results.
why_it_matters_here: A round that claims to improve a transition score or a latency only counts if the change is bigger than the noise band.
related: [p95-and-percentiles, auc, held-out-set, mutation-test, rl-environment-reward-policy]
sources:
  - Efron and Tibshirani, An Introduction to the Bootstrap, Chapman and Hall, 1993
  - Efron, "Bootstrap Methods: Another Look at the Jackknife", Annals of Statistics 7(1), 1979
  - Wikipedia, "Bootstrapping (statistics)" (secondary pointer)
status: draft
---

## In plain words

Measure the same thing twice and you get two slightly different numbers: a different sample of
tracks, timer jitter, a busy machine. The noise band is how far the number wanders when nothing
real has changed, and a change smaller than that is not evidence of anything. You can find it by
repeating the run, but repeats are slow. The bootstrap gets an estimate from the results you
already have: with n results, draw n of them at random with replacement (so some repeat and some
drop out), recompute the metric, and do that a couple of thousand times. The middle 95 percent of
those recomputed values is a 95 percent confidence interval. It needs no formula for how the
metric is distributed, which is why it suits odd metrics like a p95 or an AUC. The working rule:
call a result better only when the new interval clears the old value, or the change exceeds the
noise band measured on the same fixtures.

## Picture

```text
 score M    0.70      0.75      0.80      0.85
            +---------+---------+---------+------
 round 4    |      +======*=====+                 0.77, CI 0.74 to 0.80
 round 5a   |            +====*====+              0.79, overlaps, noise
 round 5b   |                       +===*===+     0.84, clears, real
            +---------+---------+---------+------

 the bootstrap, from five results of one round
   results       r1 r2 r3 r4 r5
   resample 1    r2 r2 r5 r1 r4    M = 0.78
   resample 2    r3 r1 r1 r5 r5    M = 0.75
   resample 3    r4 r4 r2 r3 r1    M = 0.77
   ... 2000 times, keep the middle 95 percent as the interval
```

## In Open DJ

The macro quality measure research (Tue 27 Oct 2026) makes the noise band part of every round:
each round reports its before and after on the same scorer version and fixtures with the delta
and its noise band; a round that moves M by less than the noise band twice in a row must change
hypothesis class; and AUC targets come with bootstrapped confidence intervals. The round log in
`apps/analysis_bench/rounds.py` refuses a round without controls. I did not find a shared
bootstrap helper in the code yet; it would belong beside that round log.
