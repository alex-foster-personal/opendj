---
term: Send and return
slug: send-and-return
aliases: [aux send, effects send, FX loop, send FX, parallel effect]
group: Engineering and product
level: 2
one_liner: A copy of the signal is sent to an effect by a send knob, and the effect comes back on its own path while the dry track keeps playing untouched.
why_it_matters_here: A send lets an echo or reverb outlive the channel it came from, which is how a DJ hides a gap or a clash at the end of a mix.
related: [pre-vs-post-fader, wet-dry-mix, echo-vs-delay-vs-reverb, feedback-and-ping-pong]
sources:
  - Wikipedia, Aux-send, https://en.wikipedia.org/wiki/Aux-send
  - Udo Zoelzer (ed.), DAFX Digital Audio Effects, 2nd ed., Wiley, 2011 (parallel and serial effect structures)
status: draft
---

## In plain words

An insert effect is in the path: everything goes through it. A send works in parallel. A send
knob taps a copy of the channel, at whatever amount you choose, and feeds it to an effect unit.
The effect's output comes back on a return and is added to the mix. The original track never
stops being itself, and the effect has its own level and its own life. On many DJ mixers this is
the send and return jack pair on the back for outboard gear, or a send-style setting on the
built-in FX.

The key consequence: if the send is post-fader and the return is separate, closing the channel
stops new sound entering the effect but lets what is already in it ring out.

## Picture

```text
  channel  TRIM > EQ > FADER >-----------+-----------> MIX BUS > master
                              |          dry, untouched   ^
                              |                           |
                         SEND amount                    RETURN level
                              |                           |
                              +----> ECHO or REVERB ------+

  send 0     effect hears nothing
  send 50    effect hears half
  fader down new input stops, the return keeps ringing out
```

## In Open DJ

Not yet. The only per-channel effect today is the Sound Color FX filter, which is an insert
crossfading between dry and filtered (`apps/audio-engine/src/mixer.rs`). A send would belong in
the mixer graph beside the channel fader, in `apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts`
and the Rust mixer, alongside the planned beat FX rack.
