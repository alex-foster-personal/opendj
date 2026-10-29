---
term: Trim and loudness matching
slug: trim-and-loudness-matching
aliases: [trim, gain knob, channel gain, auto gain, level matching, trim_db, trim offset, gain trim]
group: Loudness and gain
level: 1
one_liner: Trim is the per-deck gain knob at the top of a channel; matching sets the incoming track's trim so it arrives as loud as the one playing.
why_it_matters_here: Open DJ measures each track's LUFS, so the trim needed to match two tracks is a subtraction, not guesswork by ear.
related: [lufs, integrated-short-term-momentary, replaygain, headroom-and-clipping, pre-vs-post-fader, gain-ramp]
sources:
  - Pioneer DJ DJM-900NXS2 operating instructions, TRIM control and channel level indicator
  - rekordbox Operating Instructions (AlphaTheta), Auto Gain preference
  - EBU R128 (2023)
status: draft
---

## In plain words

Trim (also called gain) sits above the EQ on every mixer channel. It sets how hot the track
enters the channel before the EQ and the volume fader touch it. Its job is to make tracks
comparable, so the fader can be used for mixing rather than for fixing level.

Loudness matching is the classic move before a blend: bring the incoming track's trim to where
it feels as loud as the outgoing one, usually by watching both channel meters while listening
in headphones. With measured loudness the arithmetic is direct. Trim in dB equals the outgoing
track's LUFS minus the incoming track's LUFS:

trim_dB = LUFS_out - LUFS_in

So a -7 LUFS incoming track against a -10 LUFS outgoing track needs -3 dB. Matching by
integrated loudness is a starting point; a quiet intro or a loud drop can still call for a
touch by ear.

## Picture

```text
              DECK A (outgoing)          DECK B (incoming)
 scanned      -10 LUFS                   -7 LUFS
 trim          0 dB                      -3 dB  = -10 - (-7)

 LUFS
  -6 |                                  ####   before trim
  -7 |                                  ####
  -8 |                                  ####
  -9 |                                  ####
 -10 |=====####======================== ####==== ####  after trim
     |     ####                         ####     ####
     +------------------------------------------------
           A                            B before  B after
     signal path: in -> TRIM -> EQ -> filter -> FADER -> CROSSFADER
```

## In Open DJ

`apps/audio-engine/src/mixer.rs` maps the TRIM knob linearly from 0 to 2 times amplitude: 0.5 is
unity and the top is +6 dB (`TRIM_MAX_GAIN`), mirrored in
`apps/webui/frontend/src/lib/player/constants.ts`. `apps/audio-engine/src/deck.rs` applies trim
first, then EQ, filter, fader and crossfader, all smoothed. `apps/loudness/scan.py` computes the
gain to a target LUFS, clamped by true-peak headroom. Applying that gain to a deck's trim
automatically on load, the way rekordbox Auto Gain does, is not wired up yet.
