---
term: AudioWorklet
slug: audioworklet
aliases: [audio worklet, AudioWorkletProcessor, worklet]
group: Engineering and product
level: 3
one_liner: A small piece of custom audio code that a web page runs on the browser's audio thread, 128 samples at a time.
why_it_matters_here: Open DJ's time-stretch, level meters and dropout detector are worklets; if one misses its deadline you hear a click, so errors stop the deck rather than limp on.
related: [audio-clock-vs-page-tick, sample-accurate-scheduling, desktop-shell, ipc-dispatcher]
sources:
  - W3C, Web Audio API, AudioWorklet and the render quantum, https://www.w3.org/TR/webaudio/
  - MDN, AudioWorkletProcessor, https://developer.mozilla.org/en-US/docs/Web/API/AudioWorkletProcessor
status: draft
---

## In plain words

A browser plays sound through a chain of built-in nodes: gains, filters, players. When you need
something the browser does not ship, such as key-locked time-stretch, you write your own node.
That code runs on a separate high-priority audio thread, not the page thread that draws the
screen, and it must hand back the next 128 samples on time, every time, a few hundred times a
second. Late means a dropout. It talks to the page only by passing messages, so it never waits
on the screen.

The precise terms: an `AudioWorkletProcessor` registered with `registerProcessor`, called once
per render quantum of 128 frames by the Web Audio rendering thread.

## Picture

```text
  page thread  draws waveform, handles clicks   about 60 times a second
     |   ^
     |   |  messages only, never shared waiting
     v   |
  audio thread  every 128 frames, about 2.7 ms at 48 kHz
     source > TRIM > EQ > stretch worklet > fader > master > out
                          |
                          +> meter worklet  > levels to page

  deadline   |--- 2.7 ms ---|--- 2.7 ms ---|--- 2.7 ms ---|
  on time       ####           ####           ####
  late          ####           ######## miss  > audible click
```

## In Open DJ

Master Tempo runs through a pinned signalsmith-stretch 1.3.2 worklet, one `StretchDeckProcessor`
per deck or stem part (`docs/architecture.md`, `apps/webui/frontend/src/lib/rb/stem-graph.ts`).
Other worklets in `apps/webui/frontend/src/lib/rb/`: `meter-processor.js` for channel meters and
`xrun-sentinel-processor.js` for dropout detection; `lib/sets/master-capture-processor.js`
records the master. Worklet timeouts and processor errors are terminal, never quietly degraded.
