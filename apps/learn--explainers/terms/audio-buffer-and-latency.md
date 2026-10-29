---
term: Audio buffer and latency
slug: audio-buffer-and-latency
aliases: [buffer size, block size, frames per buffer, render quantum, output latency, audio latency]
group: Loudness and gain
level: 2
one_liner: Audio is made in blocks of frames; a block's length in milliseconds is frames divided by sample rate, and bigger blocks mean more delay.
why_it_matters_here: It sets how fast a fader move or cue press reaches the speakers, and explains why ramps are counted in frames, not buffers.
related: [gain-ramp, equal-power-crossfade, p95-and-percentiles, beat-grid]
sources:
  - W3C Web Audio API specification, render quantum and AudioContext latency (baseLatency, outputLatency)
  - Apple Core Audio Overview, I/O buffer size, developer.apple.com
  - Ken C. Pohlmann, Principles of Digital Audio, 6th ed., McGraw-Hill, 2011
status: draft
---

## In plain words

A sound card does not take one sample at a time. The engine fills a buffer of frames (one frame
is one sample for every channel, so a stereo frame is a left and right pair) and hands the whole
block over, then starts the next. Bigger buffers are safer, because the computer has more slack
before it runs dry and glitches (an underrun, heard as a crackle), but they add delay between
your hand and your ears.

The length of a buffer is frames divided by sample rate:

ms = frames / sample_rate * 1000

At 48 kHz, 1,024 frames is 21.3 ms; at 44.1 kHz it is 23.2 ms. That is why a ramp "over one
buffer" at 1,024 frames lasts about 21 ms. Total latency is usually two or three buffers in
flight plus the device and converter delay. Under about 10 ms feels instant on a jog wheel;
past about 20 ms a scratch or cue press starts to feel late.

## Picture

```text
 frames per buffer   at 44.1 kHz   at 48 kHz   feel
        128            2.9 ms        2.7 ms    instant, Web Audio
        256            5.8 ms        5.3 ms    instant
        512           11.6 ms       10.7 ms    fine
       1024           23.2 ms       21.3 ms    felt on scratch
       2048           46.4 ms       42.7 ms    late

 hand -> [ buf 1 ][ buf 2 ][ buf 3 ] -> device -> converter -> ear
          filling  queued   playing
          |<------- latency = buffers in flight + device ------->|
```

## In Open DJ

The page engine renders in Web Audio's fixed 128-frame quanta (`RENDER_QUANTUM_FRAMES` in
`apps/webui/frontend/src/lib/rb/xrun-math.ts`), and the underrun watchdog there reasons about
late quanta. The native engine in `apps/audio-engine/src/engine.rs` renders at most 1,024 frames
per pass (`MAX_BLOCK`) and chunks longer requests. Ramps are not tied to either size: the
smoothers in `apps/audio-engine/src/dsp.rs` advance per frame over 10 ms, so a fader glide
sounds the same at any buffer setting.
