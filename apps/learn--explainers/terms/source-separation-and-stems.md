---
term: Source separation and stems
slug: source-separation-and-stems
aliases: [stem separation, demixing, stems, demucs, htdemucs, unmixing, acapella extraction]
group: Harmony and analysis
level: 2
one_liner: "A neural network unbakes a finished track into vocals, drums, bass and other. Good enough to mix with, never perfectly clean."
why_it_matters_here: "Open DJ renders stems with demucs models at several quality tiers and builds vocal and structure analysis on top of them."
related: [si-sdr, vocal-activity, stem-swap, bass-swap, isolator-eq, timbre-and-mfcc]
sources:
  - "Defossez et al., Music Source Separation in the Waveform Domain, arXiv:1911.13254, 2019"
  - "Defossez, Hybrid Spectrogram and Waveform Source Separation, arXiv:2111.03600, 2021"
  - "Rouard, Massa and Defossez, Hybrid Transformers for Music Source Separation, ICASSP 2023, arXiv:2211.08553"
  - "Rafii et al., MUSDB18, a corpus for music separation, 2017"
status: draft
---

## In plain words

A mastered track is like a baked cake: the vocals, drums, bass and synths are mixed into one
waveform. Source separation tries to unbake it. Demucs, the open model most DJ tools use, was
trained on songs whose isolated multitracks are known (the MUSDB18 dataset), so it learned what
a voice, a kick or a bassline looks like in both the waveform and the spectrogram. Give it a new
track and it predicts four stems: vocals, drums, bass and "other" (everything else). The hybrid
transformer version, htdemucs, reads both views at once and is the current common default.

Separation is a guess, so it leaves fingerprints. Bleed is the most familiar: hi-hats ghosting
into the vocal stem, a sung note leaking into "other". Artifacts are the second: a watery or
phasey sound where the model was unsure, and reverb tails chopped off. The four stems summed
come close to the original but not exactly. Heavy mixes, distorted vocals and dense synths
separate worst; clean, sparse productions separate best.

## Picture

```text
   full mix   =#=#=##=#=#=##=#=#=##=#=#=##=#=#=
                       |
                    demucs
                       |
      +----------------+----------------+----------------+
      |                |                |                |
   vocals           drums            bass             other
   ..####..####..   #.#.#.#.#.#.#.   ===..===..===..  pads and leads
     .  .  .  .                       .                   .  #
     hat bleed                     kick bleed          vocal bleed

   vocals + drums + bass + other   is close to   full mix but not equal
```

## In Open DJ

`apps/stems/tiers.py` names the quality tiers and which demucs model and overlap each uses
(for example `hdemucs_mmi` and `htdemucs_ft`), with the measured evidence for each choice.
`apps/vocals/from_stems.py` fills the vocal cache from existing stem bundles, and
`apps/analysis_structure/allin1_runner.py` runs its section model on demixed audio.
