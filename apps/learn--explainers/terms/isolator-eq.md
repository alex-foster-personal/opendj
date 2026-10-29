---
term: Isolator EQ
slug: isolator-eq
aliases: [isolator, kill EQ, band isolator, crossover EQ]
group: Filters and EQ
level: 2
one_liner: A DJ EQ that splits the sound into bands with crossovers and gives each band its own volume, so a band can be turned down to silence.
why_it_matters_here: A clean bass swap needs the outgoing bass gone, which an isolator does and Open DJ's current shelf EQ does not.
related: [full-kill-eq, crossover-and-bands, linkwitz-riley-filter, bessel-filter, shelf-eq, group-delay]
sources:
  - Pioneer DJ, "DJM-900NXS2 Operating Instructions" (EQ curve selector, ISOLATOR and EQ settings), https://www.pioneerdj.com/
  - Allen and Heath, "Xone:92 User Guide" (EQ and total kill), https://www.allen-heath.com/
  - Siegfried Linkwitz, "Active Crossover Networks for Noncoincident Drivers", Journal of the Audio Engineering Society 24(1), 1976
status: draft
---

## In plain words

An isolator does not bend the sound with shelves and bells. It cuts the track into separate
bands with crossover filters, typically low, mid and high, sends each band through a plain volume
control, and adds the bands back together. With every knob at the center the bands sum back to
the original sound.

Because each band is its own path, turning a knob all the way down really removes that band: the
gain goes to zero, minus infinity in dB, and what remains is only the overlap at the crossover
slopes. That is the "kill" DJs use to drop the bass of one track while the other carries the
low end. The steeper the crossover, the cleaner the kill and the narrower the overlap, but steeper
crossovers delay some frequencies more than others, which is why their design matters.

Club rotary mixers and outboard isolator units made the sound famous; many modern club mixers
offer an isolator curve next to a classic EQ curve.

## Picture

```text
             +-> low-pass, below 250 Hz ---> x LOW gain ---+
             |                                             |
 deck in ----+-> band, 250 Hz to 2.5 kHz --> x MID gain ---+-> sum > out
             |                                             |
             +-> high-pass, above 2.5 kHz -> x HIGH gain --+

 each gain: 0 (kill, -inf dB) ... 1 (flat) ... about 2 (+6 dB)

 result with LOW at 0:
   dB   0 |                 **************************
          |               *
          |             *       <- crossover slope
   -inf   |*************
          +-------+-------+------+---------> Hz
                 100     250     1k
```

## In Open DJ

Not yet. The deck EQ is shelf, bell, shelf with a -26 dB floor (`apps/audio-engine/src/mixer.rs`).
An isolator would belong as an alternative EQ mode in the deck strip that
`apps/audio-engine/src/deck.rs` builds, run identically on both decks so their delay matches; the
transitions research lists an `eq_mode` control for it.
