---
term: Summed loudness bump
slug: summed-loudness-bump
aliases: [overlap bump, double-loud blend, midpoint bump, summing gain]
group: Loudness and gain
level: 2
one_liner: Two tracks at full gain add up, about +3 LU louder than either for different material and up to +6 dB where their peaks line up.
why_it_matters_here: Any blend with both faders up gets louder in the overlap, so an auto-transition has to pull one side down or the seam jumps out.
related: [equal-power-crossfade, headroom-and-clipping, loudness-continuity, lufs, bass-swap, trim-and-loudness-matching]
sources:
  - ITU-R BS.1770-5 (2023), summation of channel power in loudness measurement
  - Julius O. Smith III, Mathematics of the Discrete Fourier Transform, CCRMA Stanford (online book), power of sums of uncorrelated signals
  - EBU Tech 3341, Loudness Metering
status: draft
---

## In plain words

When two decks play at once, their signals add. How much louder the sum gets depends on how
alike the two signals are.

- Different tracks are uncorrelated: their waves rise and fall independently. Their powers add,
  so two equally loud tracks make a sum twice the power, +3 dB, which reads +3 LU on a loudness
  meter.
- Identical or phase-aligned material is correlated: a kick landing on both decks at the same
  instant. Their amplitudes add, so the peak doubles, +6 dB, which also eats 6 dB of headroom.

Real blends sit between the two: the beat-matched kicks are close to correlated, the pads and
vocals mostly not. That is why a long blend with both faders open feels like it surges, and why
DJs ride a fader down, swap the bass, or use an equal-power crossfader through the overlap.

uncorrelated: 10 * log10(2) = +3.01 dB    correlated: 20 * log10(2) = +6.02 dB

## Picture

```text
 LU relative to one track
 +6 |                 *   *   *         * = kick peaks, correlated
 +3 |         .======.=========.======.      uncorrelated sum
  0 |=========.                        .=============
    |  deck A alone      both full        deck B alone
 -3 |
    +---------+------------------------+-----------> time
              B fader up           A fader down

 with a fader ride or equal-power crossfade:
  0 |===============================================  flat seam
```

## In Open DJ

The mixer in `apps/audio-engine/src/mixer.rs` sums channels after each one's fader and
crossfader gain, so the bump is real whenever two faders are open; the equal-power crossfader
law removes it for uncorrelated material moved across the crossfader, not for fader-only
blends. No code measures the bump yet. It is a known trap for automatic transitions, and a proposed
rendered-transition scorer (not yet on main) is where a per-bar check would live.
