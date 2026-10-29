---
term: Offline render
slug: offline-render
aliases: [bounce, faster-than-real-time render, render preview, non-real-time render]
group: Engineering and product
level: 2
one_liner: Playing a transition into a file as fast as the computer can, instead of out of the speakers in real time, so it can be measured.
why_it_matters_here: It lets Open DJ score many candidate transitions in seconds and get the same answer every time, before anything reaches the crowd.
related: [try-er, sample-accurate-scheduling, audio-clock-vs-page-tick, rl-environment-reward-policy]
sources:
  - W3C, Web Audio API, OfflineAudioContext, https://www.w3.org/TR/webaudio/
  - Udo Zoelzer (ed.), DAFX Digital Audio Effects, 2nd ed., Wiley, 2011
status: draft
---

## In plain words

Live playback has to keep up with the clock on the wall: one second of music takes one second.
An offline render runs the same mixing math on a pretend clock that moves as fast as the CPU
allows, writing the result to a file instead of the speakers. A 30 second transition might take
a fraction of a second. Because nothing waits for the sound card, the same plan renders to the
same bytes every time, which is what you want when you are comparing options or catching a
regression.

The trade is that an offline render is a preview, not the performance. The live engine still
has to play it on the real clock, so the two should be compared, not assumed equal.

## Picture

```text
  live playback  wall clock 0s ........ 10s ........ 20s ........ 30s
                 |###############################################|
                 speakers, one pass, cannot rewind

  offline render CPU time 0s .. 0.3s
                 |###|  > mix.wav  +  metrics
                 |###|  > candidate B
                 |###|  > candidate C
                 same plan in = same bytes out, every run
```

## In Open DJ

The Rust engine renders plans offline: `odj-audio render --plan PLAN.json --out OUT.wav`, with
`--decks-out DIR` for each deck's own audio (`apps/audio-engine/src/offline.rs`,
`src/bin/odj-audio.rs`). Blocks split at every event and ramp step, so timing never depends on
block size. Besides the mix it reports which decks are heard over time, where they overlap, how
long each plays alone either side, and each deck's tempo. Today it refuses stems and beat sync,
so stem-aware transitions cannot be previewed yet.
