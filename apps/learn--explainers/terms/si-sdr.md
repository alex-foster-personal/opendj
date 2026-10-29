---
term: SI-SDR
slug: si-sdr
aliases: [scale-invariant signal-to-distortion ratio, SDR, separation quality, dB score]
group: Harmony and analysis
level: 3
one_liner: "A separation score in dB: how much of a stem is the true source versus leftovers. Higher is cleaner, and turning it up or down cannot cheat."
why_it_matters_here: "Open DJ picks its stem models by median SI-SDR against true stems, and treats differences under 0.2 dB as inaudible."
related: [source-separation-and-stems, vocal-activity, calibration-against-ratings, lufs, stem-swap]
sources:
  - "Le Roux, Wisdom, Erdogan and Hershey, SDR - half-baked or well done?, ICASSP 2019"
  - "Vincent, Gribonval and Fevotte, Performance measurement in blind audio source separation, IEEE TASLP, 2006"
  - "Rafii et al., MUSDB18, a corpus for music separation, 2017"
status: draft
---

## In plain words

To grade a separated vocal you need the real vocal to compare against, so SI-SDR is measured on
datasets like MUSDB18 where the true stems exist. The idea is simple: split the estimate into
the part that is the real source and the part that is not (bleed, artifacts, missing bits), then
report how much louder the first part is than the second, in decibels.

"Scale-invariant" means the score finds the best volume match first, so a stem that is right but
6 dB too quiet is not punished, and a model cannot win by turning its output up. The formula is
the point here:

    target = (dot(estimate, reference) / dot(reference, reference)) * reference
    SI-SDR = 10 * log10( |target|^2 / |estimate - target|^2 )

As a feel for the scale: 0 dB means the leftovers are as loud as the source; 10 dB means they are
ten times weaker in energy. Strong current models land around 9 to 10 dB on vocals. SI-SDR does
not fully match listening: a model can win on the median and lose a blind test, so it is one
signal, not the verdict.

## Picture

```text
                         * estimate
                         |
                         |  error = bleed and artifacts
                         |
   0 ====================+-----------------> reference direction
     target = best-scaled copy of the true stem

   SI-SDR = 10 log10 of  target energy  over  error energy

      0 dB    error as loud as the stem
      6 dB    error a quarter of the energy
     10 dB    error ten times weaker
```

## In Open DJ

`apps/stems/tiers.py` records the shootout on MUSDB18-HQ test tracks: `hdemucs_mmi` at a median
9.87 dB on vocals against 9.52 for the htdemucs runner-up, and notes where the blind listening test
disagreed. It also warns that SI-SDR against a lossy stem measures the codec, so scored stems stay
lossless. The 0.2 dB audibility bar is the project's own threshold.
