---
term: Timbre and MFCC
slug: timbre-and-mfcc
aliases: [tone color, sound color, MFCCs, mel-frequency cepstral coefficients, spectral envelope]
group: Harmony and analysis
level: 3
one_liner: "Timbre is why a piano and a synth playing the same note sound different. MFCCs are the dozen numbers machines use to fingerprint it."
why_it_matters_here: "Open DJ does not compute MFCCs yet; a timbre feature would let track suggestions match sound, not just key and tempo."
related: [chroma, onset-detection, source-separation-and-stems, vocal-activity, calibration-against-ratings]
sources:
  - "Davis and Mermelstein, Comparison of parametric representations for monosyllabic word recognition, IEEE TASSP, 1980"
  - "Mueller, Fundamentals of Music Processing, Springer, 2nd ed. 2021"
  - "Wikipedia, Mel-frequency cepstrum and Timbre (secondary pointers)"
status: draft
---

## In plain words

Timbre is everything about a sound that is not its pitch, loudness or length: the rasp of a
saw synth, the round thud of an 808, the breath in a voice. To a machine, most of it lives in the
spectral envelope, the overall shape of which frequencies are loud, plus how that shape moves
over a note's life.

MFCCs (mel-frequency cepstral coefficients) are the classic way to boil that shape down. Take a
short slice of audio, measure its spectrum, group the frequencies into bands spaced the way the
ear hears them (the mel scale), take the log of each band's energy, then summarize the curve with
a cosine transform and keep the first dozen or so numbers. The low coefficients describe the broad
tilt and bumps; the fine detail of individual harmonics, which carries pitch, is mostly thrown
away. That makes MFCCs a good "what does this sound like" fingerprint for genre, instrument or
similarity work, and a poor one for key, which chroma handles.

## Picture

```text
   slice > spectrum > mel bands > log > cosine transform > 13 numbers

   energy                              energy
   |  ##                               |
   |  ####                             |       ####
   |  ######  ##                       |     ########
   |  ##########  ##                   |   ############  ##
   +--------------------> freq         +--------------------> freq
     808 kick  dark and low              saw lead  bright and wide

   MFCC 1-3 capture the tilt and the big bumps   =  timbre
   fine harmonic comb discarded                  =  no pitch
```

## In Open DJ

Not yet. A search of `apps/` found no MFCC or timbre feature; the only mention is a stem tier in
`apps/stems/tiers.py` chosen so stem timbre matches across uses. It would belong as an analysis
backend under `apps/analysis/backends/`, feeding similarity in `apps/dj_copilot/candidates.py`.
