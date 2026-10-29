---
term: Feedback and ping-pong
slug: feedback-and-ping-pong
aliases: [delay feedback, regeneration, repeats, ping-pong delay, stereo bounce]
group: Engineering and product
level: 2
one_liner: Feedback sets how much of each echo is fed back to echo again, so repeats fade slowly or fast; ping-pong bounces them left and right.
why_it_matters_here: Feedback decides how long an echo out covers the gap between tracks; too much and it piles up into a wash that fights the incoming track.
related: [echo-vs-delay-vs-reverb, wet-dry-mix, send-and-return, beat-grid]
sources:
  - Udo Zoelzer (ed.), DAFX Digital Audio Effects, 2nd ed., Wiley, 2011 (delay with feedback)
  - Wikipedia, Delay (audio effect), https://en.wikipedia.org/wiki/Delay_(audio_effect)
status: draft
---

## In plain words

A delay plays the sound back a moment later. Feedback sends some of that delayed copy back into
the delay, so it repeats again, and again, each time quieter. At 50 percent feedback each repeat
is half as loud as the one before; at 90 percent the repeats hang around for a long time; at 100
or more they never die and build up. For DJ use the delay time is locked to the beat, so repeats
land on 1/2, 3/4 or 1 beat and stay musical.

Ping-pong routes the repeats so they alternate between the left and right speakers. It sounds
wider and moves, but on a mono club system the bounce collapses back to one place.

## Picture

```text
  input > + > DELAY 1 beat > out
          ^        |
          +- x0.5 -+    feedback 50 percent

  level  hit  b1    b2    b3    b4    b5
         ##   ##
         ##   ##    #
         ##   ##    #     .
  feedback 50   each repeat half the last one

  ping-pong
  left   ##         ##          #
  right        ##          #          .
         hit   b1   b2     b3   b4    b5
```

## In Open DJ

Not yet. There is no delay or echo DSP in the engine today. It would belong in the planned beat
FX rack (`apps/webui/frontend/src/lib/rb/planned-explainers.ts`), and the AutoDJ research
(`(private path)`) lists a beat-synced `fx_echo_out` primitive,
with feedback as a parameter, as the first effect to build.
