---
term: Veto versus weighted score
slug: veto-vs-weighted-score
aliases: [hard gate, hard constraint, knockout criterion, weighted sum, compensatory score]
group: Evaluation and method
level: 2
one_liner: A weighted score lets strengths make up for weaknesses; a veto zeros the score on one failure, however good the rest is.
why_it_matters_here: It decides whether a beautifully key-matched transition with flamming kicks can still win, which it must not.
related: [calibration-against-ratings, bradley-terry, mutation-test, beat-grid, camelot-wheel]
sources:
  - Keeney and Raiffa, Decisions with Multiple Objectives, Wiley, 1976 (additive value models)
  - Wikipedia, "Multiple-criteria decision analysis" (secondary pointer, compensatory versus non-compensatory methods)
status: draft
---

## In plain words

There are two ways to combine several checks into one score. A weighted score gives each term
some points and adds them up, so being strong on one can make up for being weak on another:
a perfect key match buys forgiveness for a small energy dip. That is a trade-off, and it suits
taste. A veto is a failure that is not negotiable. If the beats flam, or deck B lands on the
wrong downbeat, the crowd hears a mistake no matter how well the keys match, so one veto sets
the whole score to zero and names itself. In short:

    M = 0 if any veto fires, otherwise w1*x1 + w2*x2 + ...

Rule of thumb: veto what a crowd hears as an error; weight what is a matter of taste. Two traps:
too many vetoes and nothing passes, and a veto fed missing data must report "unknown", never
quietly pass.

## Picture

```text
 two candidates for the same pair      beat offset p95 veto at 50 ms

 candidate          key  bpm  energy  beat p95  weighted  with veto
 A smooth blend     0.9  0.9   0.8     12 ms     0.88      0.88
 B flammed kicks    1.0  1.0   1.0     64 ms     1.00      0  veto

 ranking, weighted only          ranking, vetoes first
   1  B   1.00                     1  A   0.88
   2  A   0.88                     2  B   vetoed, beat p95 64 ms

 the weighted sum alone picks the transition that sounds broken
```

## In Open DJ

The PLAY IT ordering solver, `apps/dj_copilot/solver.py`, is purely weighted: key 0.35, BPM
0.35, energy 0.25, artist 0.05. A big key jump is recorded as a `camelot_hardcut` unmet
constraint rather than a veto, a repeated artist inside the cooldown scores 0 on its term, and a
missing key or energy scores a neutral 0.5. A proposed transition scorer (not yet on main) is the other kind: a weighted geometric mean behind vetoes for beat offset,
downbeat phase and, later, live-versus-render parity, where "unknown" never passes.
