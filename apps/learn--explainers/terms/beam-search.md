---
term: Beam search
slug: beam-search
aliases: [beam width, k-best search, greedy search, pruned breadth-first search]
group: Evaluation and method
level: 2
one_liner: Beam search keeps the k best partial answers at every step instead of committing to the single best one, like greedy does.
why_it_matters_here: It is how PLAY IT orders a set today, with a beam of 8 partial orders, and it explains both why that beats greedy and where it still falls short.
related: [hamiltonian-path-and-tsp, veto-vs-weighted-score, camelot-wheel]
sources:
  - Russell and Norvig, Artificial Intelligence, A Modern Approach, 4th ed., Pearson, 2020 (local beam search)
  - Lowerre, The HARPY Speech Recognition System, PhD thesis, Carnegie Mellon University, 1976
  - Wikipedia, "Beam search" (secondary pointer)
status: draft
---

## In plain words

Greedy ordering picks the best next track and never looks back. That is fast, but the best first
move can lead into a corner: a lovely transition now that leaves only rough ones later. Beam
search keeps several partial answers alive. At each step it extends every kept partial order by
every remaining track, scores them, keeps the top k (the beam width) and drops the rest. With a
width of 1 it is greedy; with an unlimited width it is a full search that tries everything. A
width of a handful is the cheap middle: a route that looks slightly worse early can survive long
enough to win. Its limits are that a pruned route never comes back, and that by the end all the
kept routes often share the same opening, so a bad early choice can still be locked in. It
builds forward only and does not go back to polish what it already placed.

## Picture

```text
 width k = 2, numbers are the running score, higher is better

 step 1            step 2                    step 3
 S --+-- a .90 ----+-- b 1.78  keep  -------+-- c 2.60
     |             +-- c 1.70  drop
     +-- d .85 ----+-- e 1.80  keep  -------+-- b 2.70  best
     |             +-- b 1.65  drop
     +-- f .40     drop

 greedy (k = 1) keeps only a, then b, and ends S a b c at 2.60
 the beam kept d alive and found S d e b at 2.70
```

## In Open DJ

`apps/dj_copilot/solver.py` is the "greedy beam PLAY IT solver": `suggest_order` runs a beam of
width 8 by default, scoring each candidate next track with key 0.35, BPM 0.35, energy 0.25 and
artist 0.05 against the set goal's energy arc, and pruning by the summed score with a fixed tie-break, so the same
inputs always give the same order. Pinned openers, peaks and closers are
honored. The playlist-ordering research (Tue 27 Oct 2026) keeps this beam as the baseline and
plans a local-search pass after it, because the beam has no improvement step and tends to leave
the roughest transitions at the end of the set.
