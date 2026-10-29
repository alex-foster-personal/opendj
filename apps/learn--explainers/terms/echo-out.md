---
term: Echo out
slug: echo-out
aliases: [echo out, echo-out transition, feedback ramping, echo tail, delay throw out]
group: Engineering and product
level: 1
one_liner: The DJ sends the outgoing track into an echo, cuts the track, and lets the fading repeats fill the gap while the next track comes in.
why_it_matters_here: The echo_out transition style depends on it, and the tail only sounds smooth if feedback is ramped, never jumped, and the effect sits after the fader.
related: [echo-vs-delay-vs-reverb, feedback-and-ping-pong, send-and-return, wet-dry-mix, pre-vs-post-fader, lanes-and-presets]
sources:
  - Mixxx 2.5 manual, Effects chapter, https://manual.mixxx.org/2.5/en/chapters/effects
  - Mixxx 2.5 manual, Glossary, https://manual.mixxx.org/2.5/en/glossary
  - Udo Zoelzer (ed.), DAFX Digital Audio Effects, 2nd ed., Wiley, 2011 (delay with feedback)
status: draft
---

## In plain words

An echo out is a way to leave a track without a long blend. Just before the mix point the DJ
turns on an echo (a delay with feedback) on the outgoing deck, then cuts the track: fader down,
or a kill. The track stops feeding the echo, but the echo keeps playing the last beat back, each
repeat quieter than the one before, so the room hears a fading tail instead of a hole while the
next track comes in underneath.

Feedback sets how many repeats there are and how long the tail lasts. A common move is to ramp
feedback up as the fader comes down, say from 30 to 70 percent, so the tail grows longer and
smoother exactly when the dry track disappears. The ramp has to be smooth: jumping the value in
one step can click, and pushing it too high builds into a runaway wash. That is what "feedback
ramping fixes" in Mixxx notes are about: the effect moves from the old value to the new one over
a buffer instead of jumping. The echo must also sit after the fader; Mixxx's manual says its
effects are processed after the deck faders so an echo keeps sounding once its input is cut.

## Picture

```text
 level            fader cut (mix point)
                        |
 outgoing  ##########   |
 track     ##########   |
           ##########___|_________________________________
                        |
 echo                   | ##
 repeats                | ##   #
 (1 beat)               | ##   #    #
                        | ##   #    #    .    .
                        |
 feedback  30%  45%  60%  70%  70%  70%   (ramped, not jumped)
                        |
 incoming               |            ....:::::########
 track      ......:::::::::::########################
            ---------------------------------------------> time
```

## In Open DJ

Not yet. The echo_out transition style is listed in the transition plan but not built; it is
proposed, not on main, and there is no echo effect on main either. It would belong in the effects
module of the Web Audio engine under `apps/webui/frontend/src/lib/` and in the Rust engine under
`apps/audio-engine/src/`, and the Effects lane of the transition plan would carry the send and
feedback ramp alongside the fader lane.
