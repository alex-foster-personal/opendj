---
term: Rachel Bittner
slug: rachel-bittner
aliases: [Bittner, Bittner's, Bittner et al., Bittner 2017, Rachel M. Bittner, the 2017 Spotify paper]
group: People and papers
level: 1
one_liner: Spotify audio ML researcher; her 2017 playlist sequencing and transitions paper is the method floor behind this brief's reading of Spotify's Auto chooser.
why_it_matters_here: Her 2017 paper names beat-tracking errors as the number one transition failure and reports the one A/B test on record where hand-made transitions lifted daily return; both numbers anchor this work.
related: [smart-reorder-and-ordering, beat-grid, external-validation, hamiltonian-path-and-tsp, calibration-against-ratings]
sources:
  - Bittner, Gu, Hernandez, Humphrey, Jehan, McCurry, Montecchio, Automatic Playlist Sequencing and Transitions, ISMIR 2017, https://ismir2017.smcnus.org/wp-content/uploads/2017/10/86_Paper.pdf
  - Spotify newsroom, Rachel Bittner on Basic Pitch, Thu 1 Sep 2022, https://newsroom.spotify.com/2022-09-01/rachel-bittner-on-basic-pitch-an-open-source-tool-for-musicians/
  - Spotify engineering blog, Meet Basic Pitch, Jun 2022, https://engineering.atspotify.com/2022/6/meet-basic-pitch
  - GitHub, spotify/basic-pitch, https://github.com/spotify/basic-pitch
  - Bittner, Salamon, Tierney, Mauch, Cannam, Bello, MedleyDB, ISMIR 2014, https://medleydb.weebly.com/
  - GitHub, mir-dataset-loaders/mirdata, https://github.com/mir-dataset-loaders/mirdata
  - Her ISMIR papers listed at https://ir.webis.de/anthology/people/r/rachel+m.+bittner/
  - Georgia Tech Women in Music Tech speaker bio, fall 2022 (PhD at NYU MARL with Juan Pablo Bello), https://womeninmusictech.gatech.edu/seminars-and-networking-2/
status: draft
---

## In plain words

Rachel M. Bittner is a music-information-retrieval researcher at Spotify in New York, a research
manager for applied machine learning on audio by 2022 after joining as a research scientist. Her
PhD is from NYU's Music and Audio Research Laboratory (MARL), with Juan Pablo Bello.

Worth remembering:

- MedleyDB (ISMIR 2014): a multitrack dataset with pitch and melody annotations, built so melody
  extraction research had real mixes to learn from. Still a standard benchmark.
- Automatic Playlist Sequencing and Transitions (ISMIR 2017, with Gu, Hernandez, Humphrey, Jehan,
  McCurry and Montecchio, all Spotify): sequence a playlist as a path through pairwise
  compatibility, then plan each transition; beat-tracking errors were the number one failure,
  and an A/B test with hand-made transitions lifted daily return by 1.4 percentage points. This
  brief treats it as the floor on method, not a description of what Spotify ships today.
- mirdata (2019 onward, co-author): the Python library of dataset loaders that made MIR datasets
  reproducible to load, including MedleyDB.
- Basic Pitch (2022): Spotify's open-source, instrument-agnostic, polyphonic audio-to-MIDI
  converter with pitch bends, from an ICASSP 2022 paper; runs faster than real time on a laptop.

Links: the paper PDF, the Spotify newsroom interview, the Basic Pitch repository and the anthology
page of her ISMIR papers are in the sources list. A LinkedIn profile was not verified here.

## Picture

```text
 2014          2017                 2019            2022
  |             |                    |               |
  MedleyDB      Playlist sequencing  mirdata         Basic Pitch
  multitrack    and transitions      dataset         audio-to-MIDI,
  dataset       (Spotify, ISMIR)     loaders         open source
  (NYU)         beat errors = #1
                failure; +1.4 pp
                daily return A/B
  ---------------------------------------------------------------> time
                      ^
                      this brief reads Spotify's Auto chooser
                      against this paper (a floor, not a spec)
```

## In Open DJ

Not code, a reference. Section 2 of the transitions brief reads Spotify's Auto chooser against the
2017 paper and marks the link as a hypothesis; the macro quality measure research uses the paper's
A/B result as one of its external validation anchors; the Smart Reorder explainer leans on its
sequencing framing. Basic Pitch and mirdata are not used in Open DJ today.
