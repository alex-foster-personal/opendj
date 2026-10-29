---
term: Pre-fader versus post-fader
slug: pre-vs-post-fader
aliases: [pre-fader, post-fader, insert position, PFL tap]
group: Engineering and product
level: 2
one_liner: Whether an effect or a tap sits before or after the channel fader decides whether pulling the fader also kills the effect's tail.
why_it_matters_here: An echo out only works if the echo keeps ringing after the fader closes, so where the effect sits is a design choice, not a detail.
related: [send-and-return, echo-vs-delay-vs-reverb, feedback-and-ping-pong, gain-ramp]
sources:
  - AlphaTheta, DJM-900NXS2 Operating Instructions (Beat FX and channel routing), https://www.pioneerdj.com/
  - Wikipedia, Aux-send, https://en.wikipedia.org/wiki/Aux-send
status: draft
---

## In plain words

On a mixer channel the signal flows through trim, EQ and then the channel fader. An effect can
be patched in before the fader or after it. Before the fader, closing the fader silences
everything the effect produced too, so an echo dies the instant you pull down. After the fader,
the fader feeds the effect, and an echo already in its delay line keeps repeating and fading
naturally. That is the classic echo out: hit echo, pull the fader, let the repeats carry the
room into the next track.

Headphone cue is the opposite case: it listens before the fader on purpose, so you can hear a
track that the room cannot.

## Picture

```text
  PRE-FADER effect
  deck > TRIM > EQ > ECHO > FADER > master
  fader pulled at bar 8
  dry    ########|
  echo   ########|              tail cut dead
                 ^ fader closes

  POST-FADER effect, tail keeps its own output
  deck > TRIM > EQ > FADER > ECHO > master
  fader pulled at bar 8
  dry    ########|
  echo   ########|# # # . . .   repeats decay on their own
```

## In Open DJ

Not yet for effects: the beat FX rack (delay, echo, reverb) is planned, not built
(`apps/webui/frontend/src/lib/rb/planned-explainers.ts`). The taps already follow the rule:
channel meters connect post-fader (`lib/rb/meter-tap.ts`) so they show what the room hears,
and the headphone cue tap is post-EQ and pre-fader (`lib/rb/audio-engine-types.ts`). When echo
lands, it has to sit after the fader or on a send for echo out to work.
