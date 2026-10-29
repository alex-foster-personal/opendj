---
term: p95 and percentiles
slug: p95-and-percentiles
aliases: [p95, 95th percentile, p50, median, p99, tail latency]
group: Evaluation and method
level: 2
one_liner: p95 is the time that 95 of every 100 tries beat; it shows the slow tail that an average hides.
why_it_matters_here: Open DJ writes its latency targets as p95 numbers, so a rare slow deck load or a late beat counts against us instead of vanishing into an average.
related: [noise-band-and-bootstrap, audio-clock-vs-page-tick, sample-accurate-scheduling]
sources:
  - Dean and Barroso, "The Tail at Scale", Communications of the ACM 56(2), 2013
  - Hyndman and Fan, "Sample Quantiles in Statistical Packages", The American Statistician 50(4), 1996
  - Wikipedia, "Percentile" (secondary pointer)
status: draft
---

## In plain words

Sort every measurement from fastest to slowest. The p50, or median, is the one halfway down the
list; the p95 is the one 95 percent of the way down. The mean (the plain average) lets many fast
tries cancel a few awful ones: 99 deck loads of 100 ms and one of 3 s average out to 129 ms,
which looks fine, yet the DJ felt the 3 s one, mid-set. Latency is a tail problem, because the
one late beat is the one the floor hears, so we promise on p95 (sometimes p99) and quote the p50
beside it for the typical case. A percentile also needs enough samples to mean anything: a p95
taken from 10 runs is really "the second slowest run", so always state how many runs it came
from.

## Picture

```text
 count
   |
 30|   ##
 25|   ###
 20|  #####
 15|  ######
 10| ########
  5| #########  ##                       #
  0+----+----+----+----+----+----+----+----+---> ms
   0   20   40   60   80  100  120  140  160
          |       |                |
         p50     mean             p95
         32 ms   48 ms            118 ms

   most tries are quick; the tail is what a DJ actually notices
```

## In Open DJ

`(private path)` states its user-facing targets as percentiles: the library
filter keystroke-to-render target is p95 at or under 50 ms (S7), and waveform smoothness is a
p95 frame delta at or under the display refresh interval (S4). KPI names such as
`latency.input_to_audible_p95_ms` and `latency.deck_load_playable_p95_ms` carry the percentile
in the name, and the same spec's denominator-honesty rule applies to every quoted p95. The
transitions research uses p95 twice: the scorer reads beat offset between the two decks at p95,
so a single flammed kick cannot hide among good ones, and the try-er must return candidates in
p50 2.0 s and p95 3.0 s over 20 runs. New work on a latency path gets a row in
`(private path)` until it is measured.
