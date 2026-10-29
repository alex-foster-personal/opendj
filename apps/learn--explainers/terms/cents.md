---
term: Cents
slug: cents
aliases: [cent, pitch cents]
group: Time, sync and loops
level: 2
one_liner: "A cent is a hundredth of a semitone, the unit for pitch changes too small to call a note, like the drift a tempo nudge causes."
why_it_matters_here: "Tempo trims and fader moves without key lock shift pitch by cents, which decides when a stretcher is worth engaging at all."
related: [semitone-and-pitch-shift, key-lock, time-stretch, phase-correction, camelot-wheel]
sources:
  - "Ellis, On the Musical Scales of Various Nations, Journal of the Society of Arts, 1885 (origin of the cent)"
  - "Cent (music), Wikipedia (secondary pointer)"
status: draft
---

## In plain words

A semitone is one piano key. A cent is one hundredth of that, so an octave is 1200 cents.
Cents matter in DJing because tempo and pitch are tied together when key lock is off: speed a
track up by 1 percent and it goes up about 17 cents. A careful listener notices a change of
roughly 5 cents in a held tone; a change of 1 cent is far below what anyone hears in a club.

That is why a 1 cent threshold is a sensible switch for a stretcher. If the tempo ratio is within
1 cent of exactly 1.0, playing the audio straight (varispeed) shifts pitch by less than anyone
can hear, so the deck can skip the stretcher and avoid its latency, CPU and artifacts. Past
that, key lock needs the stretcher. The formula: cents = 1200 x log2(ratio), and 1 cent is a
ratio of 1.000578, about 0.058 percent.

## Picture

```text
one semitone = 100 cents          (ratio 2^(1/12) = 1.0595)
|----+----+----+----+----+----+----+----+----+----|
0   10   20   30   40   50   60   70   80   90  100   cents

|                                   1 cent    = ratio 1.000578 (0.058 %)
|==                                 5 cents   = 0.3 % tempo trim cap
|========                           17 cents  = +1 % on the pitch fader
|==================================================|  +6 % = 101 cents

below ~1 cent: inaudible, play straight, no stretcher needed
```

## In Open DJ

Not yet as a threshold: the code has no 1 cent switch. The Rust engine
(`apps/audio-engine/src/stretch.rs`) decides on the setting instead: Master Tempo off with no key
shift means no stretcher, and otherwise the stretcher always runs, even at unity, where the
quality table measures it. A cent-based bypass would belong there and in
`apps/webui/frontend/src/lib/rb/stretch-adapter.ts`. The phase lock's 0.3 percent trim cap is
about 5 cents.
