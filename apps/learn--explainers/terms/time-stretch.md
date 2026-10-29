---
term: Time-stretch
slug: time-stretch
aliases: [time stretching, time-scale modification, TSM, tempo change without pitch]
group: Time, sync and loops
level: 2
one_liner: "Changing how fast a track plays without changing its pitch, by cutting it into tiny overlapping slices and re-spacing them."
why_it_matters_here: "Key lock, sync at matched key and key shift all run through a stretcher, which adds latency, CPU and some artifacts."
related: [key-lock, stretch-engines, cents, semitone-and-pitch-shift, audio-buffer-and-latency]
sources:
  - "Zolzer (ed.), DAFX: Digital Audio Effects, 2nd ed., Wiley, 2011 (time-segment processing, phase vocoder)"
  - "Laroche and Dolson, Improved Phase Vocoder Time-Scale Modification of Audio, IEEE Trans. Speech and Audio Processing, 1999"
  - "Audio time stretching and pitch scaling, Wikipedia (secondary pointer)"
status: draft
---

## In plain words

Speed up a record and it goes up in pitch too, because the waves themselves get shorter. A time
stretcher breaks that link. It chops the sound into tiny overlapping slices (grains, tens of
milliseconds long), keeps each slice's pitch, and lays them back down slightly closer together
(faster) or further apart (slower). The trick is joining them without hearing the joins: a phase
vocoder works in the frequency domain and keeps each partial's phase continuous, while time
domain methods search for the best place to overlap.

It is not free. The stretcher must see a window of audio before it can output anything, which
is latency. It costs CPU on every deck. And it leaves artifacts: drum hits can smear, bass can
waver, and big stretches sound phasey or metallic.

## Picture

```text
input grains, read every 10.0 ms
  |=====grain 1=====|
            |=====grain 2=====|
                      |=====grain 3=====|

output, grains laid every 9.7 ms (3 % faster, same pitch)
  |=====grain 1=====|
           |=====grain 2=====|
                    |=====grain 3=====|

  varispeed instead:  every wave squeezed  ->  faster AND higher
  time-stretch:       grains re-spaced     ->  faster, same pitch
  cost: one window of latency + CPU per deck + some smear on hits
```

## In Open DJ

The stretcher is Signalsmith Stretch (MIT licensed). On the page it runs in an AudioWorklet
(`apps/webui/frontend/src/lib/rb/stretch-adapter.ts`); the Rust engine drives the same library
in 128-frame quanta (`apps/audio-engine/src/stretch.rs`). Its block size is the latency versus
quality trade: a 30 ms block would cut the scheduling floor from about 55 ms to about 20 ms, and
was refused on Tue 1 Sep 2026 because it measured worse on every quality metric.
