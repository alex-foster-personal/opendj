---
term: Key lock
slug: key-lock
aliases: [Master Tempo, MT, keylock, pitch lock]
group: Time, sync and loops
level: 1
one_liner: "With key lock on, the pitch fader changes the tempo and leaves the key alone; with it off, faster also means higher."
why_it_matters_here: "Key lock decides whether a deck goes through the time stretcher, which changes its latency, CPU cost and sound."
related: [time-stretch, stretch-engines, cents, semitone-and-pitch-shift, camelot-wheel]
sources:
  - "Pioneer DJ CDJ-3000 Operating Instructions (Master Tempo), AlphaTheta"
  - "rekordbox Operating Instructions (Master Tempo), AlphaTheta / Pioneer DJ"
  - "Serato DJ Pro Software Manual (Keylock), Serato"
status: draft
---

## In plain words

On a turntable, pushing the pitch fader up makes the record play faster and sound higher, like
a tape sped up. Key lock (Pioneer calls it Master Tempo) keeps the musical key where it was while
the tempo moves. That lets you push a track 5 percent faster to match another deck and still mix
it harmonically, and stops vocals turning into chipmunks.

Under the hood, key lock off is plain varispeed: resampling, cheap and with no delay. Key lock
on hands the deck to a time stretcher, which costs CPU and a few tens of milliseconds of latency
and can soften drums slightly. Many DJs leave it off for small changes and turn it on for big
ones. The pitch shift with key lock off is 12 x log2(tempo ratio) semitones.

## Picture

```text
pitch fader at +6 %             tempo              key
-----------------------------   ----------------   ------------------
key lock OFF  (varispeed)       128 -> 135.7 BPM   up 1.01 semitones
                                                   (8A plays near 3A)
key lock ON   (time-stretch)    128 -> 135.7 BPM   unchanged, 8A

          OFF:  tempo up  ====>  pitch up   (one knob, two effects)
          ON:   tempo up  ====>  pitch held (stretcher in the path)
```

## In Open DJ

The deck control is labeled Master Tempo. In the Rust engine
(`apps/audio-engine/src/stretch.rs`, `semitones`) the pitch handed to the stretcher is
12 x log2(tempo) with Master Tempo off and 0 with it on, plus any key shift. A Rust deck with
Master Tempo off and no key shift skips the stretcher entirely: plain varispeed has no latency and
costs about a fortieth as much. The page engine is different: every deck runs through its
Signalsmith worklet (`apps/webui/frontend/src/lib/rb/stretch-adapter.ts`) whatever the setting.
