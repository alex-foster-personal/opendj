---
term: Crossover and bands
slug: crossover-and-bands
aliases: [crossover frequency, band split, 3-band EQ, crossover point, crossover, crossovers, geometric mean]
group: Filters and EQ
level: 2
one_liner: A 3-band EQ splits the spectrum at two crossover points, by convention near 250 Hz and 2.5 kHz, into low, mid and high.
why_it_matters_here: Where the split sits decides what a bass swap removes and what the colored waveform paints as bass, mids and highs.
related: [isolator-eq, linkwitz-riley-filter, filter-order-and-slope, shelf-eq, bell-eq, full-kill-eq]
sources:
  - Siegfried Linkwitz, "Active Crossover Networks for Noncoincident Drivers", Journal of the Audio Engineering Society 24(1), 1976
  - Rane Corporation, "Linkwitz-Riley Crossovers: A Primer" (RaneNote 160), https://www.ranecommercial.com/
  - Wikipedia, "Audio crossover" (secondary pointer)
status: draft
---

## In plain words

A crossover is a pair of filters that hand the sound over from one band to the next: below the
crossover frequency the lower band takes it, above it the upper band does, and around the point
they overlap. Two crossovers make three bands.

DJ software commonly puts them near 250 Hz and 2.5 kHz. Below 250 Hz sit the kick's thump and the
bassline. Between 250 Hz and 2.5 kHz sit most of the vocal, the snare body, chords and leads.
Above 2.5 kHz sit hi-hats, cymbals, consonants and air. These are conventions, not laws: mixer
makers move them, and a kick's click lives up in the highs while a vocal's warmth reaches below
250 Hz, so no split separates instruments perfectly.

Why is the mid band's center about 791 Hz and not halfway? Three bands need exactly two split
points, low/mid at 250 Hz and mid/high at 2,500 Hz. Halfway in hertz would be 1,375 Hz, but
hearing and musical pitch work in ratios: each octave doubles the frequency. Halfway in octaves
is the geometric mean, sqrt(250 x 2500) = 791 Hz, which sits the same ratio (about 3.2 times,
or 1.66 octaves) above 250 Hz as it sits below 2,500 Hz.

A good crossover gives back the original when the bands are added up at equal gain. The usual
choice, the Linkwitz-Riley crossover, crosses each pair at -6 dB so the sum is flat.

## Picture

```text
 level
  0 dB |********.          .**********.          .*********
       |   LOW    *.     .*    MID     *.     .*    HIGH
 -6 dB |            *. .*                *. .*    <- bands cross here
       |             .*.                  .*.
       |           .*   *.              .*   *.
       |         .*       *.          .*       *.
       +--------------+----------+----------+------------> Hz
                     250        791        2.5k
                low crossover  mid      high crossover
                               center (geometric mean)

 LOW + MID + HIGH at equal gain:
  0 dB |***************************************************  flat
```

## In Open DJ

Two different splits exist. The deck EQ is not a crossover: it is a 250 Hz low shelf, a 1.2 kHz
bell and a 5 kHz high shelf (`apps/audio-engine/src/mixer.rs`), so its "bands" overlap broadly.
The three-band colored waveform is a true split at 200 Hz and 4 kHz
(`apps/audio-engine/src/waveform.rs`, `apps/analysis_waveform/decode.py`), chosen to resemble
rekordbox's display; the decode module says the numbers were stated, not discovered.
