# Stretch-quality table, lane agentB

Generated Wed 19 Aug 2026 10:34 UTC at commit `10e6f6859fc08a2db17a6a0b32950f20ff049580`. Binding spec: `.planning/QUALITY-METHODOLOGY-RECONCILED.md`.

**This table is the gate for the `STRETCH_BLOCK_MS` flip.** Per the methodology, no latency number may be quoted in any gate, PR body or status report without the same-commit quality table beside it.

## Instrument settings

| setting | value | why |
|---------|-------|-----|
| latency trim | **NONE** | amendment 3: the self-report is metadata only |
| alignment carrier | `half-wave-rectified envelope rise` | amendment 7: raw waveform cycle-skips at 0.999 confidence |
| align correlation floor (gate c) | 0.05 ON THE CARRIER | calibrated: noise 0.005, worst real arm 0.148; a raw floor is unusable because the real baseline arm reads 0.012 raw |
| reference self-similarity ceiling (gate d) | 0.95 | amendment 7 refinement: the carrier alone still cycle-skipped a sine at 0.999, so too-periodic references RAISE |
| onset basis | linear-frequency STFT flux, no mel basis (amendment 9) | amendment 9: mel defaults produce dead low bands; this harness has no mel basis and asserts the equivalent property |
| LSD floor | 80.0 dB below REFERENCE PEAK | amendment 4: absolute flooring read 39.7 dB on a null test |
| LSD frame | 50.0 ms | magnitude-spectrum comparison, phase-insensitive by construction |
| onset hop | 64 samples (1.451 ms) | amendment 5: must be <= gate/4, librosa's 512 default is 11.61 ms |
| onset recovery gate | 12.0 ms | PROPOSED, set jointly |
| pitch tracker | numpy YIN (proposed, see README) | amendment 8, proposed; see README calibration |
| measurement window | [0.5, 9.5) s | RECONSTRUCTED, see open question Q1 |

**No pass/fail threshold appears anywhere in this document.** Thresholds are set jointly across lanes after both harnesses have run these fixtures at these settings, and they ratchet per experiment-round rules. The only hard failures here are STRUCTURAL: a refusal to measure.

## Fixtures

| # | material class | stable_id | BPM | frames | rms dBFS | peak dBFS | sha256 |
|---|----------------|-----------|-----|--------|----------|-----------|--------|
| F1 | transient-heavy | `002acb181dce` | 174 | 441000 | -6.0 | 0.0 | `815ef8ec8134` |
| F2 | sustained / tonal | `0017e657b28e` | 126 | 441000 | -17.3 | -0.7 | `49f50a79df93` |
| F3 | full mix with vocal | `ad7bb55bdc2f` | 123 | 441000 | -10.4 | -0.0 | `2320042d77cf` |
| F4 | bass-heavy | `daacfd89fb24` | 122 | 441000 | -9.1 | -0.3 | `b5081fd217c1` |
| F5 | vocal-forward pop | `00b089ed7360` | 142.99 | 441000 | -16.0 | -0.2 | `b5be594c45f9` |
| F6 | acoustic / broadband | `01404696daea` | 88.30 | 441000 | -22.8 | -7.8 | `4a8af679755d` |

Every excerpt passed the loud-non-silence assert (an evicted iCloud stub serves an empty body and decodes to digital silence, so a 200 is not evidence of audio). PCM sidecars are sha256-pinned and re-verified on every read.

## Summary by arm (RANKING metrics)

| arm | cells | mean LSD dB | worst LSD dB | unity LSD dB (D1) | mean onset p95 ms | mean recovery % | node.latency() ms |
|-----|-------|-------------|--------------|-------------------|-------------------|-----------------|-------------------|
| baseline | 54 | 3.06 | 5.03 | 1.94 | 13.58 | 92.0 | 120.0 |
| block30 | 54 | 4.30 | 5.74 | 3.97 | 17.10 | 91.8 | 30.0 |
| block60 | 54 | 3.25 | 5.02 | 2.48 | 17.53 | 89.3 | 60.0 |

`node.latency()` is READ after `configure()` and reported as METADATA. It is never used to trim a render: signalsmith-stretch 1.3.2 in buffer-playback mode already self-compensates it, and trimming again puts every render 120 ms early.

## LSD by condition and arm (mean across fixtures)

| condition | rate | semis | baseline | block30 | block60 |
|-----------|------|-------|----------|---------|---------|
| C0 | 1.000 | +0 | 1.94 | 3.97 | 2.48 |
| C1 | 0.920 | +0 | 3.18 | 4.16 | 3.18 |
| C2 | 0.960 | +0 | 2.62 | 4.03 | 2.78 |
| C3 | 1.040 | +0 | 2.80 | 4.13 | 2.98 |
| C4 | 1.080 | +0 | 3.45 | 4.34 | 3.50 |
| C5 | 1.160 | +0 | 4.04 | 4.82 | 4.15 |
| C6 | 1.000 | +2 | 2.97 | 4.40 | 3.24 |
| C7 | 1.000 | -2 | 3.56 | 4.56 | 3.55 |
| C8 | 1.080 | +1 | 3.01 | 4.30 | 3.43 |

## Master tempo and key-shift accuracy (amendment 8)

Measured on the FORWARD render, never the round trip: a round trip restores the original pitch by construction. At `semis 0` the expectation is **0 cents at every rate** -- that is the master-tempo promise itself. `p50` is SIGNED so drift direction is visible.

| fixture | arm | condition | rate | expected cents | p50 cents | p95 cents | voiced % |
|---------|-----|-----------|------|----------------|-----------|-----------|----------|
| F2 | baseline | C0 | 1 | +0 | -0.1 | 3.6 | 10 |
| F2 | baseline | C6 | 1 | +200 | -0.5 | 5.3 | 10 |
| F2 | baseline | C7 | 1 | -200 | -0.4 | 5.3 | 10 |
| F2 | block60 | C0 | 1 | +0 | +0.1 | 5.3 | 10 |
| F2 | block60 | C1 | 0.920 | +0 | +0.4 | 363.5 | 10 |
| F2 | block60 | C3 | 1.040 | +0 | +0.0 | 3.9 | 10 |
| F2 | block60 | C6 | 1 | +200 | +0.2 | 5.0 | 11 |
| F2 | block60 | C7 | 1 | -200 | +0.7 | 5.5 | 10 |
| F2 | block30 | C0 | 1 | +0 | +0.9 | 4.7 | 11 |
| F2 | block30 | C1 | 0.920 | +0 | +0.7 | 425.2 | 10 |
| F2 | block30 | C2 | 0.960 | +0 | +0.8 | 663.5 | 10 |
| F2 | block30 | C3 | 1.040 | +0 | +0.8 | 899.5 | 10 |
| F2 | block30 | C6 | 1 | +200 | +1.4 | 1204.9 | 10 |
| F2 | block30 | C7 | 1 | -200 | +2.6 | 1203.8 | 10 |
| F5 | baseline | C0 | 1 | +0 | +0.2 | 13.0 | 18 |
| F5 | baseline | C1 | 0.920 | +0 | +0.4 | 29.1 | 18 |
| F5 | baseline | C2 | 0.960 | +0 | +0.3 | 50.8 | 18 |
| F5 | baseline | C3 | 1.040 | +0 | +0.3 | 50.1 | 17 |
| F5 | baseline | C4 | 1.080 | +0 | +0.2 | 12.2 | 18 |
| F5 | baseline | C5 | 1.160 | +0 | +0.4 | 31.3 | 17 |
| F5 | baseline | C6 | 1 | +200 | +0.3 | 88.2 | 18 |
| F5 | baseline | C8 | 1.080 | +100 | +0.7 | 29.2 | 18 |
| F5 | block60 | C0 | 1 | +0 | +0.6 | 18.8 | 18 |
| F5 | block60 | C1 | 0.920 | +0 | +1.1 | 41.6 | 18 |
| F5 | block60 | C2 | 0.960 | +0 | +0.9 | 42.0 | 18 |
| F5 | block60 | C3 | 1.040 | +0 | +0.7 | 35.3 | 17 |
| F5 | block60 | C4 | 1.080 | +0 | +1.0 | 20.9 | 16 |
| F5 | block60 | C5 | 1.160 | +0 | +2.7 | 75.6 | 15 |
| F5 | block60 | C6 | 1 | +200 | +1.0 | 125.2 | 18 |
| F5 | block60 | C8 | 1.080 | +100 | +1.4 | 56.3 | 18 |
| F5 | block30 | C0 | 1 | +0 | +1.6 | 53.4 | 17 |
| F5 | block30 | C1 | 0.920 | +0 | +7.1 | 981.3 | 12 |
| F5 | block30 | C2 | 0.960 | +0 | +1.7 | 65.9 | 15 |
| F5 | block30 | C3 | 1.040 | +0 | +3.6 | 60.3 | 15 |
| F5 | block30 | C4 | 1.080 | +0 | +2.9 | 68.3 | 13 |
| F5 | block30 | C5 | 1.160 | +0 | +11.9 | 1203.9 | 11 |
| F5 | block30 | C6 | 1 | +200 | +11.2 | 2668.4 | 12 |
| F5 | block30 | C8 | 1.080 | +100 | +3.3 | 64.7 | 16 |

## Full grid

> **Residual caveat (spec amendment 1).** `residual_dbr` is a DIAGNOSTIC, not a
> ranking metric, and this table contains it. Under a least-squares gain fit
> `residual dBr = 10*log10(1 - rho^2)`, so residual is a pure function of
> waveform-SHAPE similarity and nothing else. The default preset does not
> preserve waveform phase: at rate 1.000 the round-trip residual reads ~0 dBr
> at rho ~0.07, while the KNOWN-WORSE `cheaper` preset scores about 7 dB BETTER
> on residual purely by being more waveform-preserving, and loses 6/6 on LSD.
> More negative residual means "more waveform-preserving", NOT "better
> sounding". Builds are ranked by LSD + onset displacement p95 + onset
> recovery. Residual stays because it trips on waveform-domain damage -- gain
> errors, truncation, mono collapse, alignment failure -- that spectral
> measures can miss.

| fixture | arm | cond | LSD dB | onset p95 ms | recovery % | align ms | align rho | peak ratio | self-sim | residual dBr | rho | latency ms |
|---------|-----|------|--------|--------------|------------|----------|-----------|------------|----------|--------------|-----|------------|
| F1 | baseline | C0 | 2.39 | 26.12 | 93.0 | +2.00 | 0.079 | 0.740 | 0.363 | -0.0 | -0.081 | 120 |
| F1 | block30 | C0 | 4.65 | 31.20 | 90.7 | +3.36 | 0.085 | 0.667 | 0.363 | -0.0 | 0.008 | 30 |
| F1 | block60 | C0 | 2.80 | 25.40 | 93.0 | +2.72 | 0.082 | 0.576 | 0.363 | -0.0 | 0.080 | 60 |
| F1 | baseline | C1 | 4.15 | 8.71 | 96.4 | -0.54 | 0.078 | 0.700 | 0.363 | -0.0 | -0.029 | 120 |
| F1 | block30 | C1 | 4.97 | 17.41 | 93.0 | +2.72 | 0.100 | 0.464 | 0.363 | -0.1 | 0.128 | 30 |
| F1 | block60 | C1 | 3.80 | 29.02 | 87.2 | +2.09 | 0.075 | 0.751 | 0.363 | -0.0 | 0.080 | 60 |
| F1 | baseline | C2 | 3.48 | 8.71 | 95.3 | +0.54 | 0.077 | 0.576 | 0.363 | -0.1 | -0.127 | 120 |
| F1 | block30 | C2 | 4.75 | 29.02 | 90.7 | +3.08 | 0.136 | 0.439 | 0.363 | -0.0 | 0.046 | 30 |
| F1 | block60 | C2 | 3.41 | 30.48 | 88.4 | +3.63 | 0.095 | 0.461 | 0.363 | -0.0 | -0.019 | 60 |
| F1 | baseline | C3 | 3.48 | 8.71 | 95.3 | +1.63 | 0.111 | 0.539 | 0.363 | -0.0 | -0.080 | 120 |
| F1 | block30 | C3 | 4.90 | 10.16 | 95.3 | +2.99 | 0.116 | 0.606 | 0.363 | -0.0 | -0.022 | 30 |
| F1 | block60 | C3 | 3.94 | 22.13 | 93.0 | +0.09 | 0.081 | 0.652 | 0.363 | -0.0 | 0.041 | 60 |
| F1 | baseline | C4 | 4.49 | 5.80 | 98.8 | -0.18 | 0.085 | 0.648 | 0.363 | -0.0 | 0.055 | 120 |
| F1 | block30 | C4 | 5.19 | 16.33 | 93.0 | +2.72 | 0.098 | 0.562 | 0.363 | -0.0 | -0.061 | 30 |
| F1 | block60 | C4 | 4.69 | 25.69 | 92.0 | -0.82 | 0.078 | 0.583 | 0.363 | -0.0 | 0.026 | 60 |
| F1 | baseline | C5 | 5.03 | 20.90 | 89.5 | +3.17 | 0.065 | 0.726 | 0.363 | -0.0 | 0.031 | 120 |
| F1 | block30 | C5 | 5.74 | 25.03 | 93.0 | +3.63 | 0.105 | 0.545 | 0.363 | -0.0 | -0.072 | 30 |
| F1 | block60 | C5 | 5.02 | 21.19 | 90.7 | +3.17 | 0.095 | 0.521 | 0.363 | -0.0 | 0.089 | 60 |
| F1 | baseline | C6 | 3.80 | 29.75 | 91.9 | +3.45 | 0.093 | 0.436 | 0.363 | -0.0 | -0.002 | 120 |
| F1 | block30 | C6 | 5.40 | 10.16 | 95.3 | +3.54 | 0.091 | 0.624 | 0.363 | -0.0 | -0.081 | 30 |
| F1 | block60 | C6 | 4.15 | 30.48 | 89.5 | +1.90 | 0.109 | 0.651 | 0.363 | -0.0 | 0.105 | 60 |
| F1 | baseline | C7 | 4.42 | 26.12 | 92.0 | -1.27 | 0.083 | 0.537 | 0.363 | -0.0 | 0.008 | 120 |
| F1 | block30 | C7 | 5.33 | 8.34 | 96.5 | +2.81 | 0.081 | 0.652 | 0.363 | -0.0 | -0.014 | 30 |
| F1 | block60 | C7 | 4.10 | 30.48 | 89.5 | +2.18 | 0.105 | 0.526 | 0.363 | -0.0 | 0.026 | 60 |
| F1 | baseline | C8 | 4.11 | 7.26 | 94.2 | +3.90 | 0.060 | 0.665 | 0.363 | -0.0 | -0.050 | 120 |
| F1 | block30 | C8 | 5.24 | 5.44 | 97.7 | +3.36 | 0.086 | 0.506 | 0.363 | -0.0 | 0.064 | 30 |
| F1 | block60 | C8 | 3.99 | 13.42 | 94.2 | +3.27 | 0.071 | 0.755 | 0.363 | -0.0 | -0.031 | 60 |
| F2 | baseline | C0 | 1.72 | 4.35 | 98.7 | +3.72 | 0.156 | 0.296 | 0.077 | -0.1 | -0.167 | 120 |
| F2 | block30 | C0 | 3.70 | 4.35 | 98.7 | +2.81 | 0.147 | 0.166 | 0.077 | -0.0 | -0.011 | 30 |
| F2 | block60 | C0 | 2.24 | 4.35 | 98.7 | +3.27 | 0.148 | 0.216 | 0.077 | -0.0 | 0.036 | 60 |
| F2 | baseline | C1 | 2.66 | 2.90 | 96.1 | +2.36 | 0.086 | 0.343 | 0.077 | -0.1 | -0.142 | 120 |
| F2 | block30 | C1 | 3.80 | 4.35 | 100.0 | +2.63 | 0.167 | 0.170 | 0.077 | -0.0 | -0.032 | 30 |
| F2 | block60 | C1 | 2.89 | 4.35 | 97.4 | +2.45 | 0.111 | 0.315 | 0.077 | -0.0 | -0.095 | 60 |
| F2 | baseline | C2 | 2.34 | 4.35 | 98.7 | +1.90 | 0.102 | 0.345 | 0.077 | -0.0 | 0.055 | 120 |
| F2 | block30 | C2 | 3.66 | 3.34 | 98.7 | +2.45 | 0.135 | 0.161 | 0.077 | -0.0 | -0.017 | 30 |
| F2 | block60 | C2 | 2.47 | 4.35 | 97.4 | +2.54 | 0.127 | 0.288 | 0.077 | -0.0 | 0.001 | 60 |
| F2 | baseline | C3 | 2.52 | 4.35 | 97.4 | +2.63 | 0.119 | 0.300 | 0.077 | -0.1 | -0.128 | 120 |
| F2 | block30 | C3 | 3.75 | 2.90 | 100.0 | +2.81 | 0.165 | 0.166 | 0.077 | -0.0 | -0.082 | 30 |
| F2 | block60 | C3 | 2.61 | 2.90 | 97.4 | +2.90 | 0.098 | 0.278 | 0.077 | -0.0 | -0.012 | 60 |
| F2 | baseline | C4 | 2.98 | 4.79 | 97.4 | +3.27 | 0.124 | 0.429 | 0.077 | -0.0 | 0.054 | 120 |
| F2 | block30 | C4 | 3.94 | 4.72 | 97.4 | +2.54 | 0.120 | 0.218 | 0.077 | -0.0 | -0.036 | 30 |
| F2 | block60 | C4 | 3.18 | 4.35 | 98.7 | +4.72 | 0.095 | 0.424 | 0.077 | -0.0 | -0.056 | 60 |
| F2 | baseline | C5 | 3.66 | 7.26 | 96.1 | +1.63 | 0.095 | 0.406 | 0.077 | -0.0 | -0.043 | 120 |
| F2 | block30 | C5 | 4.36 | 4.35 | 96.1 | +2.81 | 0.124 | 0.229 | 0.077 | -0.0 | 0.089 | 30 |
| F2 | block60 | C5 | 3.85 | 4.35 | 98.7 | +3.45 | 0.093 | 0.329 | 0.077 | -0.0 | 0.093 | 60 |
| F2 | baseline | C6 | 2.69 | 4.79 | 97.4 | +2.00 | 0.096 | 0.256 | 0.077 | -0.0 | -0.046 | 120 |
| F2 | block30 | C6 | 3.99 | 4.79 | 96.1 | +3.27 | 0.101 | 0.222 | 0.077 | -0.0 | 0.047 | 30 |
| F2 | block60 | C6 | 3.05 | 4.35 | 98.7 | +3.45 | 0.074 | 0.454 | 0.077 | -0.0 | -0.051 | 60 |
| F2 | baseline | C7 | 3.30 | 5.80 | 97.4 | +4.44 | 0.066 | 0.618 | 0.077 | -0.0 | -0.036 | 120 |
| F2 | block30 | C7 | 4.22 | 4.35 | 94.7 | +2.27 | 0.112 | 0.192 | 0.077 | -0.0 | 0.002 | 30 |
| F2 | block60 | C7 | 3.38 | 4.35 | 98.7 | +2.36 | 0.073 | 0.345 | 0.077 | -0.0 | -0.010 | 60 |
| F2 | baseline | C8 | 2.31 | 4.35 | 97.4 | +2.90 | 0.123 | 0.275 | 0.077 | -0.0 | 0.088 | 120 |
| F2 | block30 | C8 | 3.81 | 2.90 | 100.0 | +2.81 | 0.163 | 0.167 | 0.077 | -0.0 | -0.105 | 30 |
| F2 | block60 | C8 | 2.61 | 4.35 | 100.0 | +1.45 | 0.102 | 0.419 | 0.077 | -0.0 | 0.041 | 60 |
| F3 | baseline | C0 | 2.24 | 13.64 | 92.3 | +3.17 | 0.222 | 0.628 | 0.382 | -0.1 | 0.118 | 120 |
| F3 | block30 | C0 | 4.26 | 19.95 | 87.2 | +2.45 | 0.245 | 0.642 | 0.382 | -0.1 | 0.160 | 30 |
| F3 | block60 | C0 | 2.81 | 22.86 | 91.0 | +4.26 | 0.294 | 0.632 | 0.382 | -0.0 | 0.085 | 60 |
| F3 | baseline | C1 | 4.10 | 17.27 | 89.9 | -2.09 | 0.200 | 0.779 | 0.382 | -0.2 | -0.204 | 120 |
| F3 | block30 | C1 | 4.51 | 22.86 | 82.1 | +2.27 | 0.260 | 0.661 | 0.382 | -0.0 | -0.064 | 30 |
| F3 | block60 | C1 | 3.41 | 13.35 | 92.3 | +1.72 | 0.238 | 0.689 | 0.382 | -0.0 | -0.080 | 60 |
| F3 | baseline | C2 | 2.78 | 11.90 | 93.6 | +2.54 | 0.176 | 0.767 | 0.382 | -0.0 | -0.098 | 120 |
| F3 | block30 | C2 | 4.36 | 17.41 | 88.5 | +1.81 | 0.192 | 0.860 | 0.382 | -0.0 | 0.044 | 30 |
| F3 | block60 | C2 | 2.97 | 7.47 | 97.4 | +2.90 | 0.265 | 0.666 | 0.382 | -0.1 | -0.117 | 60 |
| F3 | baseline | C3 | 3.15 | 9.14 | 92.3 | +1.36 | 0.244 | 0.628 | 0.382 | -0.7 | 0.384 | 120 |
| F3 | block30 | C3 | 4.45 | 18.87 | 89.7 | +3.36 | 0.341 | 0.701 | 0.382 | -0.2 | 0.190 | 30 |
| F3 | block60 | C3 | 3.30 | 22.13 | 85.9 | +0.91 | 0.182 | 0.829 | 0.382 | -0.0 | -0.016 | 60 |
| F3 | baseline | C4 | 3.59 | 15.96 | 89.7 | +3.72 | 0.196 | 0.766 | 0.382 | -0.0 | 0.098 | 120 |
| F3 | block30 | C4 | 4.69 | 13.28 | 93.6 | +2.90 | 0.240 | 0.620 | 0.382 | -0.1 | 0.168 | 30 |
| F3 | block60 | C4 | 3.66 | 9.58 | 92.3 | +2.36 | 0.191 | 0.763 | 0.382 | -0.0 | -0.028 | 60 |
| F3 | baseline | C5 | 4.41 | 12.05 | 91.0 | +2.63 | 0.186 | 0.824 | 0.382 | -0.0 | -0.025 | 120 |
| F3 | block30 | C5 | 5.48 | 13.93 | 89.7 | +6.80 | 0.243 | 0.842 | 0.382 | -0.2 | -0.223 | 30 |
| F3 | block60 | C5 | 4.55 | 30.84 | 88.5 | +3.63 | 0.220 | 0.700 | 0.382 | -0.1 | 0.166 | 60 |
| F3 | baseline | C6 | 3.06 | 13.35 | 92.3 | +1.81 | 0.206 | 0.777 | 0.382 | -0.0 | -0.031 | 120 |
| F3 | block30 | C6 | 4.97 | 14.73 | 93.6 | +6.26 | 0.185 | 0.874 | 0.382 | -0.1 | -0.119 | 30 |
| F3 | block60 | C6 | 3.48 | 26.56 | 93.6 | +3.45 | 0.197 | 0.737 | 0.382 | -0.0 | -0.054 | 60 |
| F3 | baseline | C7 | 4.12 | 18.50 | 91.0 | +6.44 | 0.199 | 0.866 | 0.382 | -0.1 | -0.118 | 120 |
| F3 | block30 | C7 | 4.93 | 14.51 | 88.5 | +3.90 | 0.240 | 0.723 | 0.382 | -0.0 | 0.054 | 30 |
| F3 | block60 | C7 | 3.93 | 18.29 | 87.2 | +0.27 | 0.200 | 0.732 | 0.382 | -0.1 | 0.159 | 60 |
| F3 | baseline | C8 | 3.21 | 14.88 | 89.7 | +3.17 | 0.271 | 0.650 | 0.382 | -0.5 | -0.324 | 120 |
| F3 | block30 | C8 | 4.96 | 15.67 | 92.3 | +6.62 | 0.224 | 0.915 | 0.382 | -0.0 | 0.021 | 30 |
| F3 | block60 | C8 | 3.34 | 11.32 | 93.6 | +2.72 | 0.270 | 0.681 | 0.382 | -0.0 | -0.022 | 60 |
| F4 | baseline | C0 | 1.70 | 27.57 | 90.3 | +2.99 | 0.233 | 0.470 | 0.521 | -0.0 | 0.028 | 120 |
| F4 | block30 | C0 | 3.68 | 27.28 | 90.3 | +2.63 | 0.201 | 0.594 | 0.521 | -0.0 | -0.033 | 30 |
| F4 | block60 | C0 | 2.50 | 23.22 | 91.3 | -0.09 | 0.143 | 0.867 | 0.521 | -0.0 | -0.027 | 60 |
| F4 | baseline | C1 | 2.37 | 29.02 | 88.3 | +3.08 | 0.194 | 0.519 | 0.521 | -0.0 | -0.051 | 120 |
| F4 | block30 | C1 | 3.73 | 28.44 | 90.3 | +2.36 | 0.186 | 0.647 | 0.521 | -0.0 | -0.028 | 30 |
| F4 | block60 | C1 | 2.90 | 26.34 | 89.3 | +4.99 | 0.125 | 0.879 | 0.521 | -0.0 | 0.007 | 60 |
| F4 | baseline | C2 | 2.17 | 21.04 | 93.2 | +1.81 | 0.185 | 0.694 | 0.521 | -0.0 | -0.035 | 120 |
| F4 | block30 | C2 | 3.72 | 26.05 | 91.3 | +2.81 | 0.201 | 0.559 | 0.521 | -0.0 | -0.007 | 30 |
| F4 | block60 | C2 | 2.54 | 27.57 | 89.3 | +1.00 | 0.208 | 0.813 | 0.521 | -0.0 | 0.007 | 60 |
| F4 | baseline | C3 | 2.27 | 8.71 | 93.2 | +3.17 | 0.192 | 0.550 | 0.521 | -0.0 | 0.010 | 120 |
| F4 | block30 | C3 | 3.73 | 29.02 | 89.3 | +2.54 | 0.195 | 0.646 | 0.521 | -0.0 | -0.019 | 30 |
| F4 | block60 | C3 | 2.50 | 26.12 | 88.3 | +3.17 | 0.259 | 0.633 | 0.521 | -0.0 | 0.016 | 60 |
| F4 | baseline | C4 | 2.65 | 27.36 | 91.3 | +2.81 | 0.173 | 0.576 | 0.521 | -0.0 | -0.058 | 120 |
| F4 | block30 | C4 | 3.86 | 30.48 | 87.4 | +2.90 | 0.196 | 0.570 | 0.521 | -0.0 | -0.002 | 30 |
| F4 | block60 | C4 | 2.80 | 24.67 | 89.3 | +3.17 | 0.222 | 0.603 | 0.521 | -0.0 | 0.038 | 60 |
| F4 | baseline | C5 | 3.21 | 30.84 | 88.3 | +2.09 | 0.179 | 0.712 | 0.521 | -0.1 | -0.137 | 120 |
| F4 | block30 | C5 | 4.15 | 25.98 | 90.3 | +1.63 | 0.156 | 0.691 | 0.521 | -0.0 | -0.067 | 30 |
| F4 | block60 | C5 | 3.40 | 24.67 | 90.3 | +4.08 | 0.134 | 0.683 | 0.521 | -0.0 | 0.014 | 60 |
| F4 | baseline | C6 | 2.39 | 36.72 | 85.4 | +1.81 | 0.164 | 0.555 | 0.521 | -0.0 | -0.056 | 120 |
| F4 | block30 | C6 | 3.74 | 20.03 | 91.3 | +2.99 | 0.167 | 0.584 | 0.521 | -0.0 | -0.015 | 30 |
| F4 | block60 | C6 | 2.66 | 29.02 | 85.4 | +2.54 | 0.167 | 0.537 | 0.521 | -0.0 | -0.072 | 60 |
| F4 | baseline | C7 | 2.93 | 26.12 | 86.4 | +0.18 | 0.162 | 0.772 | 0.521 | -0.0 | -0.003 | 120 |
| F4 | block30 | C7 | 3.99 | 23.22 | 90.3 | +1.45 | 0.159 | 0.608 | 0.521 | -0.0 | 0.007 | 30 |
| F4 | block60 | C7 | 2.96 | 29.24 | 83.5 | +1.90 | 0.155 | 0.580 | 0.521 | -0.1 | 0.111 | 60 |
| F4 | baseline | C8 | 2.50 | 19.45 | 90.3 | +5.44 | 0.141 | 0.707 | 0.521 | -0.0 | 0.014 | 120 |
| F4 | block30 | C8 | 3.67 | 27.50 | 91.3 | +2.90 | 0.204 | 0.568 | 0.521 | -0.0 | -0.023 | 30 |
| F4 | block60 | C8 | 4.82 | 30.33 | 49.1 | -9.52 | 0.155 | 0.769 | 0.521 | -0.0 | -0.038 | 60 |
| F5 | baseline | C0 | 1.83 | 4.35 | 96.2 | +2.18 | 0.295 | 0.759 | 0.360 | -0.0 | 0.005 | 120 |
| F5 | block30 | C0 | 3.65 | 18.07 | 90.6 | +2.45 | 0.242 | 0.738 | 0.360 | -0.1 | -0.148 | 30 |
| F5 | block60 | C0 | 2.19 | 9.72 | 92.5 | +2.18 | 0.345 | 0.677 | 0.360 | -0.1 | -0.151 | 60 |
| F5 | baseline | C1 | 2.87 | 11.32 | 92.5 | +4.08 | 0.271 | 0.710 | 0.360 | -0.1 | -0.110 | 120 |
| F5 | block30 | C1 | 3.88 | 17.27 | 88.7 | +2.45 | 0.289 | 0.716 | 0.360 | -0.1 | -0.131 | 30 |
| F5 | block60 | C1 | 2.95 | 13.71 | 90.6 | +2.36 | 0.304 | 0.671 | 0.360 | -0.1 | 0.121 | 60 |
| F5 | baseline | C2 | 2.38 | 10.16 | 92.5 | +2.09 | 0.284 | 0.912 | 0.360 | -0.0 | 0.021 | 120 |
| F5 | block30 | C2 | 3.76 | 14.51 | 90.6 | +2.09 | 0.237 | 0.793 | 0.360 | -0.0 | 0.007 | 30 |
| F5 | block60 | C2 | 2.56 | 5.80 | 92.5 | +2.45 | 0.263 | 0.754 | 0.360 | -0.0 | -0.051 | 60 |
| F5 | baseline | C3 | 2.59 | 9.43 | 92.5 | +2.18 | 0.289 | 0.663 | 0.360 | -0.0 | -0.063 | 120 |
| F5 | block30 | C3 | 3.86 | 20.68 | 88.7 | +1.81 | 0.235 | 0.783 | 0.360 | -0.0 | -0.061 | 30 |
| F5 | block60 | C3 | 2.65 | 15.02 | 90.6 | +2.63 | 0.288 | 0.706 | 0.360 | -0.2 | 0.189 | 60 |
| F5 | baseline | C4 | 3.62 | 13.93 | 94.3 | -1.36 | 0.279 | 0.785 | 0.360 | -0.0 | 0.027 | 120 |
| F5 | block30 | C4 | 4.07 | 18.14 | 86.8 | +2.90 | 0.213 | 0.831 | 0.360 | -0.1 | -0.108 | 30 |
| F5 | block60 | C4 | 3.32 | 18.58 | 88.7 | +0.54 | 0.262 | 0.853 | 0.360 | -0.0 | 0.039 | 60 |
| F5 | baseline | C5 | 3.85 | 18.07 | 88.7 | +1.27 | 0.321 | 0.666 | 0.360 | -0.1 | 0.166 | 120 |
| F5 | block30 | C5 | 4.56 | 27.57 | 83.0 | +4.99 | 0.233 | 0.806 | 0.360 | -0.0 | 0.013 | 30 |
| F5 | block60 | C5 | 3.92 | 19.52 | 86.8 | +3.08 | 0.258 | 0.781 | 0.360 | -0.1 | 0.122 | 60 |
| F5 | baseline | C6 | 3.18 | 7.91 | 94.3 | +5.53 | 0.253 | 0.825 | 0.360 | -0.1 | -0.147 | 120 |
| F5 | block30 | C6 | 4.09 | 15.96 | 86.8 | +2.72 | 0.268 | 0.765 | 0.360 | -0.0 | 0.018 | 30 |
| F5 | block60 | C6 | 3.05 | 18.14 | 86.8 | +2.45 | 0.313 | 0.664 | 0.360 | -0.0 | -0.047 | 60 |
| F5 | baseline | C7 | 3.34 | 18.14 | 86.8 | +0.27 | 0.266 | 0.826 | 0.360 | -0.0 | -0.039 | 120 |
| F5 | block30 | C7 | 4.39 | 29.32 | 83.0 | +1.63 | 0.220 | 0.919 | 0.360 | -0.0 | -0.069 | 30 |
| F5 | block60 | C7 | 3.39 | 19.52 | 84.9 | +2.72 | 0.230 | 0.801 | 0.360 | -0.0 | -0.058 | 60 |
| F5 | baseline | C8 | 3.02 | 12.34 | 90.6 | +5.53 | 0.231 | 0.908 | 0.360 | -0.0 | -0.001 | 120 |
| F5 | block30 | C8 | 3.96 | 36.21 | 86.8 | +3.17 | 0.304 | 0.714 | 0.360 | -0.0 | 0.101 | 30 |
| F5 | block60 | C8 | 2.81 | 8.27 | 92.5 | +3.72 | 0.239 | 0.803 | 0.360 | -0.1 | -0.131 | 60 |
| F6 | baseline | C0 | 1.75 | 4.21 | 91.7 | +3.17 | 0.358 | 0.257 | 0.142 | -1.2 | -0.500 | 120 |
| F6 | block30 | C0 | 3.90 | 8.27 | 95.8 | +2.72 | 0.229 | 0.252 | 0.142 | -0.0 | 0.032 | 30 |
| F6 | block60 | C0 | 2.32 | 2.90 | 91.7 | +2.81 | 0.251 | 0.265 | 0.142 | -0.4 | 0.283 | 60 |
| F6 | baseline | C1 | 2.95 | 5.66 | 91.7 | +2.54 | 0.168 | 0.405 | 0.142 | -0.0 | 0.030 | 120 |
| F6 | block30 | C1 | 4.08 | 21.26 | 91.7 | +2.72 | 0.204 | 0.283 | 0.142 | -0.0 | -0.035 | 30 |
| F6 | block60 | C1 | 3.10 | 27.86 | 83.3 | +2.45 | 0.192 | 0.354 | 0.142 | -0.1 | -0.145 | 60 |
| F6 | baseline | C2 | 2.55 | 29.90 | 87.5 | +2.54 | 0.254 | 0.367 | 0.142 | -0.7 | 0.393 | 120 |
| F6 | block30 | C2 | 3.95 | 5.59 | 95.8 | +2.81 | 0.221 | 0.220 | 0.142 | -0.0 | -0.089 | 30 |
| F6 | block60 | C2 | 2.74 | 17.41 | 79.2 | +2.18 | 0.184 | 0.296 | 0.142 | -0.2 | -0.201 | 60 |
| F6 | baseline | C3 | 2.79 | 5.66 | 91.7 | +2.72 | 0.169 | 0.422 | 0.142 | -0.0 | 0.017 | 120 |
| F6 | block30 | C3 | 4.06 | 30.69 | 91.7 | +3.17 | 0.202 | 0.275 | 0.142 | -0.0 | -0.082 | 30 |
| F6 | block60 | C3 | 2.86 | 2.90 | 91.7 | +2.99 | 0.172 | 0.449 | 0.142 | -0.0 | 0.098 | 60 |
| F6 | baseline | C4 | 3.35 | 8.71 | 83.3 | +3.36 | 0.180 | 0.457 | 0.142 | -0.0 | 0.014 | 120 |
| F6 | block30 | C4 | 4.26 | 25.54 | 91.7 | +3.08 | 0.199 | 0.263 | 0.142 | -0.0 | 0.000 | 30 |
| F6 | block60 | C4 | 3.37 | 13.06 | 79.2 | +2.45 | 0.159 | 0.396 | 0.142 | -0.0 | -0.096 | 60 |
| F6 | baseline | C5 | 4.11 | 20.32 | 79.2 | +3.45 | 0.149 | 0.734 | 0.142 | -0.1 | -0.183 | 120 |
| F6 | block30 | C5 | 4.67 | 26.85 | 87.5 | +2.99 | 0.175 | 0.363 | 0.142 | -0.0 | 0.047 | 30 |
| F6 | block60 | C5 | 4.18 | 27.57 | 75.0 | +1.90 | 0.149 | 0.613 | 0.142 | -0.1 | 0.117 | 60 |
| F6 | baseline | C6 | 2.71 | 7.26 | 83.3 | +2.18 | 0.215 | 0.419 | 0.142 | -0.1 | -0.181 | 120 |
| F6 | block30 | C6 | 4.24 | 10.09 | 87.5 | +2.18 | 0.154 | 0.369 | 0.142 | -0.0 | 0.057 | 30 |
| F6 | block60 | C6 | 3.07 | 7.26 | 83.3 | +2.18 | 0.157 | 0.494 | 0.142 | -0.0 | 0.053 | 60 |
| F6 | baseline | C7 | 3.21 | 2.90 | 95.8 | +3.72 | 0.136 | 0.749 | 0.142 | -0.0 | 0.067 | 120 |
| F6 | block30 | C7 | 4.52 | 9.87 | 91.7 | +2.18 | 0.146 | 0.369 | 0.142 | -0.0 | 0.022 | 30 |
| F6 | block60 | C7 | 3.51 | 21.77 | 70.8 | +0.63 | 0.149 | 0.500 | 0.142 | -0.0 | 0.040 | 60 |
| F6 | baseline | C8 | 2.90 | 4.35 | 83.3 | +3.17 | 0.229 | 0.280 | 0.142 | -0.1 | 0.158 | 120 |
| F6 | block30 | C8 | 4.17 | 9.29 | 95.8 | +2.72 | 0.187 | 0.276 | 0.142 | -0.0 | 0.012 | 30 |
| F6 | block60 | C8 | 3.00 | 27.86 | 83.3 | +2.72 | 0.182 | 0.333 | 0.142 | -0.0 | 0.099 | 60 |

## Calibration and structural checks

| check | result | note |
|-------|--------|------|
| S1 determinism | **PASS** | every cell rendered TWICE in-page and the two sha256s compared; inequality FAILS the run rather than being recorded |
| S2 channel distinctness | **PASS** | distinctness, not channel count: a mono-collapsed render still reports two |
| S3 alignment integrity | **PASS** | the amendment-7 four-gate stack: (a) rise carrier at both stages, (b) coarse and fine bound raise, (c) correlation floor ON THE CARRIER, (d) reference self-similarity ceiling; plus the degenerate-carrier and peak-dominance guards. A refusal is reported, never replaced by a number |
| S4 loud non-silence | **PASS** | asserted on every excerpt and every render |

### D1 unity transparency

D1 gates on **unity LSD**, not null residual: the default preset scrambles phase at rate 1.000 (residual ~0 dBr at rho ~0.07), so a residual-based D1 would disqualify every build including baseline. Lane A's baseline calibration read **1.14 dB**. Measured here:

| arm | unity LSD dB (C0, mean across fixtures) | vs lane A baseline calibration |
|-----|-----------------------------------------|--------------------------------|
| baseline | 1.94 | +0.80 |
| block30 | 3.97 | +2.83 |
| block60 | 2.48 | +1.34 |

No build in this tree bypasses the stretcher at exactly 1.000, so the stronger bit-identity claim (sha256 passthrough) does not apply and the unity LSD row stands.

### D2-D7

**DEFINITIONS UNAVAILABLE.** The reconciled spec adopts lane B's base proposal for the self-disqualification clauses and re-bases D1 explicitly, but that base proposal is not present in this repository or its git history, so only D1 has a stated definition. Reported rather than invented (open question Q1). The observables this harness produces that D2-D7 would plausibly consume are all in `results.json`: `lsd_db`, `onset_displacement_p95_ms`, `onset_recovery`, `pitch_error_cents_p50/p95`, `residual_dbr`, `align_correlation`.

Cells measured: 162. Cells refused: 0. 
Fixtures: 6. Arms: 3. Conditions: 9.
