---
term: Wet/dry mix
slug: wet-dry-mix
aliases: [dry/wet, mix knob, depth, level/depth, wet level]
group: Engineering and product
level: 1
one_liner: The knob that blends the untouched track (dry) with the effect's output (wet), from no effect at all to effect only.
why_it_matters_here: It is how every effect in a transition fades in and out without a jump, and Open DJ's color filter already works this way inside.
related: [send-and-return, feedback-and-ping-pong, equal-power-crossfade, isolator-eq]
sources:
  - Udo Zoelzer (ed.), DAFX Digital Audio Effects, 2nd ed., Wiley, 2011
  - AlphaTheta, DJM-900NXS2 Operating Instructions (Beat FX LEVEL/DEPTH), https://www.pioneerdj.com/
status: draft
---

## In plain words

Dry is the track as it is. Wet is what comes out of the effect. The mix knob decides how much of
each you hear. Fully dry, the effect is bypassed in sound even if it is running. Fully wet, you
hear only the effect, which for an echo means only repeats. Most DJ effects live somewhere in
between, and riding this knob is half of playing an effect well.

On a hardware mixer the same control is often labeled LEVEL/DEPTH. The precise form is a
crossfade between two signals, and like any crossfade it can be linear or equal-power, which
changes whether the middle sounds quieter.

## Picture

```text
  dry ----------+------------------+
                |                  |
                +> EFFECT > wet ---+> MIX knob > out

  knob     dry level     wet level
    0      ##########    .
   25      ########      ###
   50      ######        ######
   75      ###           ########
  100      .             ##########

  0 = track only     50 = both     100 = effect only
```

## In Open DJ

The Sound Color FX filter in the Rust engine is a wet/dry blend: `FilterParams` in
`apps/audio-engine/src/mixer.rs` carries a `dry` gain and two wet gains, one for the low-pass
side and one for the high-pass side. At center the knob is fully dry; turning it fades the dry
path out and the selected filter in. The planned beat FX rack (delay, echo, reverb) will need the
same control, and transition plans would ramp it like any other knob.
