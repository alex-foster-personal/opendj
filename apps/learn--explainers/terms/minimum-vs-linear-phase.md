---
term: Minimum phase vs linear phase
slug: minimum-vs-linear-phase
aliases: [minimum phase, linear phase, linear-phase EQ, FIR vs IIR phase]
group: Filters and EQ
level: 3
one_liner: Minimum-phase filters react instantly but bend timing per frequency; linear-phase filters keep timing even but add latency and pre-ringing.
why_it_matters_here: A live DJ EQ must be minimum phase for low latency, so its timing smear is a design fact to manage, not a bug to fix.
related: [group-delay, zero-phase-filtering, bessel-filter, biquad, filter-order-and-slope]
sources:
  - Julius O. Smith III, "Introduction to Digital Filters with Audio Applications" (minimum phase, linear phase), CCRMA Stanford, https://ccrma.stanford.edu/~jos/filters/
  - Richard G. Lyons, "Understanding Digital Signal Processing", 3rd edition, Prentice Hall, 2010
  - Bob Katz, "Mastering Audio: The Art and the Science", Focal Press, 2015 (linear-phase equalizers)
status: draft
---

## In plain words

Feed a filter a single click and watch what comes out: that is its impulse response, and its
shape tells you the filter's phase family.

A minimum-phase filter puts its energy out as early as possible. The click is answered at once,
then the tail dies away. There is no added latency, which is why every analog EQ and every biquad
in a DJ mixer works this way. The cost is that the delay differs by frequency, so phase bends
around each cutoff and transients are slightly smeared.

A linear-phase filter answers with a symmetric shape centered some time later. Every frequency is
delayed by exactly the same amount, so a drum hit keeps its shape. But the filter has to wait half
its own length before answering, often tens of milliseconds for bass, and the symmetric response
puts a little ringing before each hit as well as after it, which is audible as pre-echo on steep
low cuts. Mastering engineers use it; live mixing cannot afford the wait.

## Picture

```text
 IMPULSE RESPONSE: what one click looks like after the filter

 minimum phase                    linear phase
    |*                               |            *
    |**                              |           ***
    | * *                            |        . * * * .
    |   *  .  .                      | . . . .  *   *  . . . .
    +-----*----.--.--> time          +------------------------> time
    0                                0            delay
    answers at once, no wait         waits half its length,
    delay differs per frequency      rings before and after,
                                     same delay at every frequency
```

## In Open DJ

Every live filter is minimum phase: the shelves, bell, low-pass and high-pass are IIR biquads in
`apps/audio-engine/src/dsp.rs`. No linear-phase FIR filter exists in the live path. The only
phase-neutral filtering is the offline zero-phase pass in `scripts/bench/r1-offline/`.
