---
term: ReplayGain
slug: replaygain
aliases: [RG, track gain, album gain, volume normalization tag]
group: Loudness and gain
level: 2
one_liner: ReplayGain is a loudness tag stored in a file that tells a player how much to turn that track up or down so tracks play at an even level.
why_it_matters_here: Open DJ measures loudness the same way but applies the result as a mixer trim the DJ can see and override, not as a hidden playback tag.
related: [lufs, trim-and-loudness-matching, true-peak, headroom-and-clipping]
sources:
  - ReplayGain 2.0 specification, wiki.hydrogenaud.io (ReplayGain 2.0 specification page)
  - Mixxx User Manual 2.5, glossary and Normalization preferences, manual.mixxx.org
  - ITU-R BS.1770-5 (2023)
status: draft
---

## In plain words

ReplayGain is a player-side normalization scheme from the early 2000s. A scanner measures each
track once and writes two numbers into its tags: a gain in dB (track gain, or album gain for a
whole album played together) and the track's peak. The player reads the tag at playback and
scales the audio, without changing the file. Version 2.0 uses the BS.1770 loudness measurement
with a reference of -18 LUFS, so a track at -12 LUFS gets a tag of about -6 dB.

LUFS trim does the same arithmetic. The differences are where it lives and who sees it.
ReplayGain is silent and global: every track arrives levelled, and the tag can be wrong or
missing. A DJ trim is a visible knob per deck, set from the measured LUFS against a chosen
target, which the DJ can ride during the mix. ReplayGain also has no say in a blend; it levels
tracks in isolation, not two tracks playing at once.

## Picture

```text
  ReplayGain (tag, at playback)        LUFS trim (knob, on the deck)

  file -> [tag: -6.0 dB] -> player     file -> scan: -12 LUFS
            |                                    |
            v                                    v
        scale x0.50 -> out             target -18: trim = -6 dB
        (hidden, global)               TRIM knob shows it -> mixer
                                       DJ can ride it  ---> fader

  level   before   after               level   before   after
   -12  |  ####                         -12  |  ####
   -18  |  ####    ####  = reference    -18  |  ####    ####  = target
        +-------------                       +-------------
```

## In Open DJ

Open DJ does not read or write ReplayGain tags. `apps/loudness/scan.py` derives a
"ReplayGain-style" gain to a caller-supplied target from its own BS.1770 scan, clamped so the
result cannot push true peak past the ceiling. The intended consumer is the per-deck trim, as
rekordbox and Serato do with auto-gain. Mixxx, by contrast, applies ReplayGain-based
normalization by default, per its manual.
