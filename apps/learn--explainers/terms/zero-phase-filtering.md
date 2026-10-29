---
term: Zero-phase filtering
slug: zero-phase-filtering
aliases: [filtfilt, sosfiltfilt, forward-backward filtering, two-pass filtering]
group: Filters and EQ
level: 3
one_liner: Running a filter forward and then backward over a whole file so its time shifts cancel; perfect for analysis, impossible during live playback.
why_it_matters_here: Open DJ's offline benchmarks use it to measure stems without timing smear, while the live engine must use ordinary one-way filters.
related: [group-delay, minimum-vs-linear-phase, bessel-filter, filter-order-and-slope]
sources:
  - SciPy documentation, "scipy.signal.filtfilt" and "scipy.signal.sosfiltfilt", https://docs.scipy.org/doc/scipy/reference/signal.html
  - Fredrik Gustafsson, "Determining the initial states in forward-backward filtering", IEEE Transactions on Signal Processing 44(4), 1996
  - Julius O. Smith III, "Introduction to Digital Filters with Audio Applications" (forward-backward filtering), CCRMA Stanford, https://ccrma.stanford.edu/~jos/filters/
status: draft
---

## In plain words

Every ordinary filter shifts some frequencies later in time (see group delay). Zero-phase filtering
cancels that shift with a trick: filter the whole signal forward, flip the result end to end,
filter it again, and flip it back. The second pass shifts everything the opposite way by exactly
the same amount, so nothing moves in time at any frequency. The level change is applied twice, so
the slope doubles.

The catch is that the backward pass needs the samples that come after the moment being computed.
With a file already on disk that is fine. During playback those samples have not happened yet, so
the only way to fake it is to wait, which turns into latency a DJ cannot accept. That is why
filtfilt, the name MATLAB and SciPy give the function, is an offline tool for analysis, stem
measurement and display, never for a live knob. The ends of the signal also need padding, or the
first and last moments come out distorted.

## Picture

```text
  whole track already in memory
  |==========================================>   time

  pass 1: filter forward    ------------------>    shifts lows later
  flip end to end
  pass 2: filter backward   <------------------    shifts them back
  flip back

  result: no time shift at any frequency, cut applied twice

  LIVE PLAYBACK
                              now
                               |
  =========> already played    |   future: not known yet
                               |
           pass 2 needs these -+------------>   cannot run live
```

## In Open DJ

Offline only. `scripts/bench/r1-offline/r1d_stem_leakage.py` and
`scripts/bench/r1-offline/r1_inst_and_metric.py` call SciPy's `sosfiltfilt` to measure low-band
energy in separated stems without timing smear. The live engine (`apps/audio-engine/src/dsp.rs`)
runs ordinary causal biquads.
