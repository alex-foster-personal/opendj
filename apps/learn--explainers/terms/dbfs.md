---
term: dBFS
slug: dbfs
aliases: [decibels relative to full scale, dB full scale, digital level]
group: Loudness and gain
level: 1
one_liner: dBFS measures a digital level against the loudest value the system can store; 0 dBFS is the ceiling and every real signal reads below it.
why_it_matters_here: Every meter, clip light and gain knob in Open DJ is expressed in dBFS, so it is the shared ruler for the whole mixer.
related: [true-peak, headroom-and-clipping, lufs, trim-and-loudness-matching]
sources:
  - AES17-2020, AES standard method for digital audio engineering, measurement of digital audio equipment (definition of full scale)
  - IEC 60268-10, Peak programme level meters
  - "Wikipedia: dBFS, https://en.wikipedia.org/wiki/DBFS (secondary pointer)"
status: draft
---

## In plain words

A digital audio file stores each sample as a number with a hard top: the biggest value the
format can hold. dBFS ("decibels relative to full scale") says how far a level sits below that
top. 0 dBFS is the ceiling itself, so every useful reading is negative: -6 dBFS is half the
maximum amplitude, -20 dBFS is one tenth. Nothing can be louder than 0 dBFS inside a fixed-point
file; anything that tries is cut off flat, which is clipping.

The decibel is a ratio, so the same rule works everywhere: each -6 dB halves the amplitude, and
each -20 dB divides it by ten.

dB = 20 * log10(amplitude / full_scale)

## Picture

```text
 amplitude   dBFS
   1.000  ---  0  ===========  ceiling: nothing stored above this
   0.891  ---  -1             mastering true-peak target
   0.708  ---  -3
   0.500  ---  -6  ......     half the amplitude
   0.251  ---  -12
   0.100  ---  -20 ......     one tenth
   0.032  ---  -30
   0.001  ---  -60 ---------  Open DJ meter floor (reads empty)

     -60 ............ -20 ........ -6 ... -1  0
     |                 |           |      |  |#  clip light
     +-----------------+-----------+------+--+
          quiet             music         hot
```

## In Open DJ

The channel and master meters work in dBFS: `apps/webui/frontend/src/lib/rb/meter-math.ts`
sets the scale floor at -60 dBFS (`METER_FLOOR_DBFS`) and the latched clip light at -0.1 dBFS
(`CLIP_DBFS`), with segments spaced in dB rather than in raw amplitude so the bottom of the bar
still moves for real music. The offline scanner in `apps/loudness/scan.py` reports true peak
in dBTP alongside the loudness figure. Internally the mixer computes in
floating point, which can briefly exceed 1.0; the ceiling bites where audio is written to a
fixed-point file, as in the recorder and WAV writer in `apps/audio-engine/src/`, which clamp to
the range -1 to 1.
