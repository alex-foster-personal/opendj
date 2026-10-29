---
term: Stem swap
slug: stem-swap
aliases: [stem transition, part swap, acapella over, Neural Mix swap]
group: Engineering and product
level: 2
one_liner: Handing one part, such as the drums, bass or vocals, from one deck to the other during a transition while the rest keeps playing.
why_it_matters_here: Stems let a blend avoid two vocals or two basslines meeting, which EQ alone cannot separate cleanly.
related: [source-separation-and-stems, bass-swap, lanes-and-presets, offline-render]
sources:
  - Algoriddim, djay Pro manual, Neural Mix, https://help.algoriddim.com/
  - Wikipedia, Music source separation, https://en.wikipedia.org/wiki/Music_source_separation
status: draft
---

## In plain words

Source separation splits a track into stems: usually vocals, drums, bass and everything else.
With stems on both decks you can swap one part at a time. Bring B's drums in under A's vocal,
swap the bass on a downbeat, then let A's vocal finish before B's starts. An EQ bass swap does
roughly the same with frequency bands; a stem swap does it with the actual instrument, so a kick
and a bassline in the same low band can be moved separately.

The catch is quality. Separated stems carry artifacts, and a swap that is near-instant needs both
decks' stems lined up to the sample, or you hear a flam.

## Picture

```text
  bars           1   2   3   4 | 5   6   7   8
                               |
  A vocals    #################|#####.........
  A drums     ##########.......|..............
  A bass      #################|..............
                               |
  B drums     ..........#######|##############
  B bass      .................|##############
  B vocals    .................|.......#######
                               ^
          drums swap bar 3, bass swap on the mix point,
          vocals never overlap
```

## In Open DJ

Partly. Stem playback exists: `apps/webui/frontend/src/lib/rb/stem-graph.ts` gives each stem
part its own stretch processor and gain, summed at TRIM, and refuses to start if parts do not
line up. A stem swap transition does not exist yet. The AutoDJ research lists `stem_swap` as a
planned style, and the offline renderer refuses stems today, so a stem swap cannot be previewed
before it plays.
