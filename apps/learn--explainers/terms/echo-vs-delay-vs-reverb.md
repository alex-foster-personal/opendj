---
term: Echo versus delay versus reverb
slug: echo-vs-delay-vs-reverb
aliases: [echo out, delay, reverb wash, tail effects]
group: Engineering and product
level: 1
one_liner: Delay repeats the sound; echo is a delay with fading, beat-locked repeats; reverb smears it into a room-like wash with no distinct repeats.
why_it_matters_here: Each leaves a different tail when the track stops, so each hides a different kind of bad transition.
related: [feedback-and-ping-pong, pre-vs-post-fader, send-and-return, wet-dry-mix]
sources:
  - Udo Zoelzer (ed.), DAFX Digital Audio Effects, 2nd ed., Wiley, 2011
  - AlphaTheta, DJM-900NXS2 Operating Instructions (Beat FX ECHO, DELAY, REVERB), https://www.pioneerdj.com/
status: draft
---

## In plain words

All three add sound after the original, but the shape of the tail differs. A delay plays clear
copies at a set time; with little feedback it might be one or two slaps. An echo on a DJ mixer
is a delay tuned for leaving: beat-locked repeats that fade away over a bar or two, so you can
stop a track and its rhythm keeps ticking out. Reverb has no separate repeats at all. It is
thousands of tiny reflections blended into a wash, like singing in a hall, and it hides rhythm
rather than repeating it.

In a mix, echo out exits a track cleanly on the beat, reverb wash covers a tempo or key mismatch
by blurring it, and a short delay adds groove without leaving.

## Picture

```text
  track stops at the bar line  |
                               |
  dry      ####################|
                               |
  delay    ####################| #           #  one or two clear slaps
                               |
  echo     ####################| ##  #   .   .  beat-locked, fading
                               |
  reverb   ####################|########....... smooth wash, no repeats
                               |
                         beat  1   2   3   4
```

## In Open DJ

Not yet. None of the three exists as DSP in the engine; the FX panel that would hold them is a
planned control (`apps/webui/frontend/src/lib/rb/planned-explainers.ts`). The AutoDJ research
(`(private path)`) gives echo out 1 to 4 bars and names it the
universal escape for a pair that will not blend, so it is the first to build, then reverb.
