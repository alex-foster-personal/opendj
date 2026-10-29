---
term: Taper law
slug: taper-law
aliases: [taper, knob law, pot taper, audio taper, log taper, linear taper, control curve]
group: Filters and EQ
level: 2
one_liner: The rule that turns a knob's position into a value; audio taper makes equal turns sound like equal steps, linear taper does not.
why_it_matters_here: Open DJ chose a separate law for each knob (EQ linear in dB, filter even in octaves, trim linear in gain), and those choices shape how a mix feels.
related: [cutoff-frequency, full-kill-eq, dbfs, gain-ramp, equal-power-crossfade, shelf-eq]
sources:
  - Rod Elliott, "Beginners' Guide to Potentiometers", Elliott Sound Products, https://sound-au.com/pots.htm
  - Wikipedia, "Potentiometer", audio taper section (secondary pointer)
  - Open DJ, (private path), Thu 20 Aug 2026
status: draft
---

## In plain words

A knob only reports where it is, say 0 to 100 percent. The taper law decides what that position
means. With a linear taper, halfway is half the value. With an audio taper (also called log taper,
or an "A" pot), halfway is only around a tenth to a fifth of the amplitude, roughly -14 to -20 dB.

Audio taper exists because hearing is roughly logarithmic: we judge loudness in ratios, so equal
turns should give equal steps in dB, not equal steps in amplitude. A linear volume knob does
almost everything in the first quarter of its travel and very little after that.

The same idea applies to frequency. A filter knob that moved its cutoff in equal hertz would
spend most of its turn above 10 kHz, where little seems to happen, then rush through the bass. A
law that is even in octaves (exponential in Hz) makes every part of the turn feel the same.

## Picture

```text
 amplitude
 100% |                                                *
      |                                        L   L **
      |                                    L       **
      |                            L   L        ***
  50% |                        L             ***
      |                L   L            *****
      |            L              ******
      |    L   L      ************
   0% |***************
      +-----------+-----------+-----------+-----------+-> knob
      0%         25%         50%         75%        100%
 L = linear taper: half a turn is half the amplitude
 * = audio taper: half a turn is about 15 percent, near -16 dB
```

## In Open DJ

`eq_db_from_knob` in `apps/audio-engine/src/mixer.rs` is linear in dB in two pieces: knob 0 is
-26 dB, center 0 dB, full +6 dB. The filter knob is exponential in Hz with a 2 percent center dead
zone (`filterParamsFromKnob` in `apps/webui/frontend/src/lib/rb/audio-engine-guards.ts`; reasoning
in `(private path)`). Trim is linear in amplitude, 0 to 2x with unity at the
center.
