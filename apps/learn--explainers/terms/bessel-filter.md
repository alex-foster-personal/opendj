---
term: Bessel filter
slug: bessel-filter
aliases: [Bessel-Thomson filter, Thomson filter, maximally flat delay filter]
group: Filters and EQ
level: 3
one_liner: A filter tuned so every frequency it passes is delayed by the same amount, keeping drum hits sharp at the price of a soft, slow roll-off.
why_it_matters_here: It is one of two common shapes for isolator crossovers, picked when keeping the kick transient intact matters more than a steep split.
related: [group-delay, butterworth-filter, linkwitz-riley-filter, isolator-eq, minimum-vs-linear-phase, filter-order-and-slope]
sources:
  - W. E. Thomson, "Delay networks having maximally flat frequency characteristics", Proceedings of the IEE, Part III, 96(44), 1949
  - Julius O. Smith III, "Introduction to Digital Filters with Audio Applications", CCRMA Stanford, https://ccrma.stanford.edu/~jos/filters/
  - Wikipedia, "Bessel filter" (secondary pointer)
status: draft
---

## In plain words

Every filter delays sound a little. Most filters delay frequencies near their cutoff more than the
rest, so a sharp attack, made of many frequencies at once, comes out with its parts slightly out
of step: it overshoots and rings. A Bessel filter is designed the other way round. It keeps the
delay as even as possible across the passband (maximally flat group delay), so a kick or a snare
keeps its shape and does not ring.

The price is the magnitude response. A Bessel filter bends over gently and slowly near the
cutoff, so for the same order it separates bands less sharply than a Butterworth, and you need a
higher order to get the same isolation.

It still costs time: an even delay is still a delay. For a 4th-order Bessel low-pass with its
-3 dB point at 100 Hz, every passband frequency arrives about 3 ms late. Higher order and lower
cutoff both make it longer.

## Picture

```text
 STEP RESPONSE: a sharp edge after a 4th-order low-pass

 Butterworth                       Bessel
 1.1 |      **                     1.1 |
 1.0 |     *  ** **********        1.0 |       *************
     |    *     *                      |     **
 0.5 |   *                         0.5 |    *
     |  *                              |   *
 0.0 |**                           0.0 |***
     +--------------> time             +--------------> time
     overshoots, then rings            rises cleanly, no ring

 GROUP DELAY across frequency (how late each frequency arrives)
 Butterworth  ------------.**.------   bump near the cutoff
 Bessel       ----------------------   even until past the cutoff
```

## In Open DJ

Not yet. No Bessel filter exists in the engine or the analysis code. It would belong in an
isolator EQ mode in the deck strip (`apps/audio-engine/src/deck.rs`), or in an analysis band split
where onset timing must survive the filter.
