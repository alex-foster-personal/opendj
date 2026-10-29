---
term: Semitone and pitch shift
slug: semitone-and-pitch-shift
aliases: [half step, key shift, transpose, pitch shifting, key change]
group: Harmony and analysis
level: 2
one_liner: "A semitone is one piano key. Shifting a track a semitone or two can make two keys fit; more than two starts to sound processed."
why_it_matters_here: "Each Open DJ deck carries a key shift of -12 to +12 semitones on top of the analyzed key, which never changes."
related: [camelot-wheel, fifths-and-fourths, energy-boost-mix, key-lock, time-stretch, key-clash]
sources:
  - "Mixed In Key, Harmonic Mixing book, https://mixedinkey.com/Book/"
  - "Zoelzer (ed.), DAFX: Digital Audio Effects, Wiley, 2nd ed. 2011, pitch shifting chapter"
  - "Wikipedia, Semitone and Pitch shift (secondary pointers)"
status: draft
---

## In plain words

A semitone is the smallest step in Western music: one piano key to the next, white or black.
Twelve of them make an octave. In frequency terms each semitone multiplies the pitch by the
twelfth root of 2, about 1.0595, so it is a 5.9 percent change. Speed a record up 6 percent with
key lock off and it plays one semitone higher.

Pitch shifting moves a track's key without changing its tempo. It is how a DJ makes two
neighboring-but-wrong keys fit: if 8A meets 3A, shifting the second track down one semitone turns
it into 8A. The surprise is that one semitone is a big jump on the wheel: it moves seven steps,
because the wheel counts fifths. The rule is wheel steps = 7 x semitones, wrapped around 12.

Beyond about two semitones the shifter's tricks start to show. Voices change character
(chipmunk up, giant down) because their formants move with the pitch, transients smear, and
basslines can lose weight. One semitone is usually transparent, two is a judgment call, three
or more is an effect rather than a fix.

## Picture

```text
   piano  | C |C#| D |D#| E | F |F#| G |G#| A |A#| B | C |
          +---+--+---+--+---+---+--+---+--+---+--+---+---+
          <-1->  one semitone = one key = x 1.0595 = 5.9 pct

   start at 8A = A minor
   shift   lands on   wheel move
   -2      6A         2 steps      energy drop
   -1      1A         7 steps      far on the wheel
    0      8A         home
   +1      3A         7 steps      far on the wheel
   +2      10A        2 steps      energy boost
   +3 or more         artifacts likely
```

## In Open DJ

The deck state in `apps/webui/frontend/src/lib/rb/deck-state-types.ts` stores
`key_shift_semitones` from -12 to +12; the source key stays immutable so a shift never rewrites
analysis. The Jaws note in `apps/shared/harmonic.py` records the same arithmetic: -5 wheel steps
is one semitone up.
