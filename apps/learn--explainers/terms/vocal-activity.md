---
term: Vocal activity
slug: vocal-activity
aliases: [vocalness, vocal regions, vocal detection, singing voice detection, VAD]
group: Harmony and analysis
level: 2
one_liner: "Where in a track someone is singing, measured bar by bar. Two vocals at once almost always clash, whatever the keys."
why_it_matters_here: "Open DJ derives vocal regions from the separated vocal stem and checks them against rekordbox's own vocal markers."
related: [source-separation-and-stems, section-and-phrase-detection, key-clash, stem-swap, si-sdr, bar-phase]
sources:
  - "Defossez, Hybrid Spectrogram and Waveform Source Separation, arXiv:2111.03600, 2021"
  - "AlphaTheta, rekordbox manual, phrase and vocal analysis sections"
  - "Wikipedia, Voice activity detection (secondary pointer)"
status: draft
---

## In plain words

Vocal activity answers a simple question for every moment of a track: is someone singing or
rapping right now, and how strongly? The usual machine recipe is to separate the vocal stem
first, measure its loudness in short hops, compare that to the full mix or a fixed floor, and
call the stretches above threshold vocal regions. Rounded to bars, that gives a DJ a map: bars
1 to 16 instrumental, 17 to 32 verse, and so on. "Vocalness" is the continuous version, a 0 to
1 score per bar instead of a yes or no.

Two vocals at once clash for reasons beyond key. The ear follows words, and two sets of words
fight for the same attention, so both become hard to understand. Their melodies rarely share a
key or rhythm, and they sit in the same frequency band, so EQ cannot pull them apart. That is why
DJs plan blends so one vocal ends before the next starts, or run an acapella over an instrumental
on purpose.

## Picture

```text
   bar       0   4   8   12  16  20  24  28  32
             |---|---|---|---|---|---|---|---|
   deck A    ....####################.........    A vocal bars 5 to 24
   deck B    ................#################    B vocal from bar 17
   overlap                   ********             bars 17 to 24 clash

   better
   deck A    ....############.................    A vocal ends bar 16
   deck B    ........................#########    B vocal from bar 25

   # = vocal region   . = instrumental   * = two vocals at once
```

## In Open DJ

`apps/vocals/envelope.py` stores per-hop `vocals_rms` and `mix_rms` beside the vocal cache, since
vocal regions are a pure function of them. `apps/parity/vocal.py` scores rekordbox's vocal regions
against Open DJ's demucs-derived ones by overlap on a 10 Hz grid. Using vocal overlap to steer
transition suggestions: not yet; it would belong in `apps/dj_copilot`.
