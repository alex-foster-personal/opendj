---
term: Audio clock versus page tick
slug: audio-clock-vs-page-tick
aliases: [two clocks, AudioContext.currentTime, requestAnimationFrame timing, UI timer]
group: Time, sync and loops
level: 3
one_liner: "The audio clock counts samples and is exact; a page timer ticks about every 16 ms and wakes up late, so cuts timed from it land late."
why_it_matters_here: "Open DJ schedules every transport change on the audio clock and only uses page ticks to draw what was already heard."
related: [sample-accurate-scheduling, audio-buffer-and-latency, quantize, phase-correction, ipc-dispatcher]
sources:
  - "Wilson, A Tale of Two Clocks: Scheduling Web Audio with Precision, HTML5 Rocks / web.dev, 2013"
  - "Web Audio API, W3C Recommendation (AudioContext.currentTime, getOutputTimestamp, AudioWorklet render quantum), https://www.w3.org/TR/webaudio/"
status: draft
---

## In plain words

A browser app has two clocks. The audio clock is driven by the sound card: it counts samples,
48,000 a second, and never wobbles. The page has timers and animation frames, which tick roughly
every 16.7 ms on a 60 Hz screen and fire whenever the main thread gets round to them, sometimes
much later if the page is busy.

If code waits for a page tick to notice "the beat has passed, cut now", the cut is late by
however far the tick was past the beat, plus the time for the message to reach the audio
thread, plus one audio block. That is 10 to 30 ms, and a DJ hears 10 ms as a flam. The fix is to
look ahead: on each tick, schedule anything due soon at its exact time on the audio clock, and
let the audio thread fire it.

## Picture

```text
time (ms)   980       990       1000      1010      1020      1030
            |---------|---------|---------|---------|---------|
the beat                        #  <- where the cut belongs
page tick             *                *                *
                      990              1007             1024
                      "not yet"        "passed, cut now"
                                       + message + one audio block:
                                         heard near 1015, 15 ms late

audio clock: the cut was scheduled ahead for t = 1000.000 ms and is
             heard exactly there, whatever the page was doing
```

## In Open DJ

`docs/architecture/audio-timing.md` sets the rule. `AudioContext.currentTime` is for planning:
commands are scheduled ahead (`SYNC_SCHEDULE_SAFETY_S = 0.1`), and a command counts only once the
stretch worklet acknowledges it. What the deck shows comes from `getOutputTimestamp()`, the
"what has the listener actually heard" clock, read once per animation frame. So the page tick
draws the playhead but never decides when sound happens, and the interface never claims a cue,
loop or tempo change happened before it was audible.
