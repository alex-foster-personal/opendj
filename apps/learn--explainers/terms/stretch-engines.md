---
term: Stretch engines
slug: stretch-engines
aliases: [time-stretch library, Rubber Band, Rubber Band R3, multithreaded Rubber Band, SoundTouch, Signalsmith Stretch, R2, R3]
group: Time, sync and loops
level: 3
one_liner: "The libraries that do time-stretching differ in method, sound, CPU cost, latency and license; picking one is a trade, not a ranking."
why_it_matters_here: "The engine sets how key lock sounds, how late a synced deck can be scheduled, and what license the app can ship under."
related: [time-stretch, key-lock, audio-buffer-and-latency, semitone-and-pitch-shift]
sources:
  - "Rubber Band Library documentation (R2 Faster and R3 Finer engines, licensing), Breakfast Quay, https://breakfastquay.com/rubberband/"
  - "Mixxx 2.5 manual, Sound Hardware preferences (Keylock/Pitch-Bending Engine selector), https://manual.mixxx.org/2.5/en/chapters/preferences/sound_hardware.html"
  - "SoundTouch Audio Processing Library documentation, Olli Parviainen, https://www.surina.net/soundtouch/"
  - "Signalsmith Stretch, README and design notes, Signalsmith Audio, https://github.com/Signalsmith-Audio/signalsmith-stretch"
  - "Verhelst and Roelands, An Overlap-Add Technique Based on Waveform Similarity (WSOLA), ICASSP 1993"
status: draft
---

## In plain words

Every time-stretcher answers the same question, "how do I make this longer or shorter without
changing its pitch?", with a different recipe, and each recipe costs something different.
SoundTouch works in the time domain: it finds where overlapping slices of waveform match best
and splices them. It is light on CPU and fine on speech and gentle changes, but grainier on dense
music. Rubber Band uses a phase vocoder. Its R2 engine (documented as "Faster") was the long
standing default; its R3 engine ("Finer", from version 3) analyzes at several resolutions at
once, which keeps both bass and transients cleaner but costs clearly more CPU. Signalsmith Stretch
is a newer phase vocoder designed for real time use with a small, MIT licensed codebase.

R3 is Rubber Band's finer engine, added in version 3; R2 is the older one. "Multithreaded Rubber
Band", as in Mixxx notes, means the stretcher's per-channel work is spread over more than one CPU
thread, so the heavier R3 engine can still keep up in real time.

License is part of the choice: SoundTouch is LGPL 2.1, Rubber Band is GPL or a paid commercial
license, Signalsmith Stretch is MIT, which is why Open DJ uses it rather than Rubber Band.

## Picture

```text
engine              method              sound         CPU     license
------------------  ------------------  ------------  ------  ----------
SoundTouch          time domain, WSOLA  fair, grainy  low     LGPL 2.1
                    splice at best fit  on dense mix
Rubber Band R2      phase vocoder       good          medium  GPL or
  "Faster"                                                    commercial
Rubber Band R3      phase vocoder,      best of the   high    GPL or
  "Finer"           multi-resolution    four                  commercial
Signalsmith Stretch phase vocoder,      good          medium  MIT
  (Open DJ today)   real-time design

latency: every engine needs a window of audio first; a bigger window
usually means better bass and steadier tone, and a later deck
```

Ratings are relative and come from the vendors' own descriptions, not from a measurement here.

## In Open DJ

Open DJ ships Signalsmith Stretch on both engines: the page's AudioWorklet
(`apps/webui/frontend/src/lib/rb/stretch-adapter.ts`) and the Rust engine
(`apps/audio-engine/src/stretch.rs`), which calls the upstream library and does not reimplement
it. Quality is measured, not assumed: a committed table of stretch conditions
(`ops/quality/stretch/report.md`) scores block sizes by log spectral distance. Rubber Band and
SoundTouch are not wired in.
