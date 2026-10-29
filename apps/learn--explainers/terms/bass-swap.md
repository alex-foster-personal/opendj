---
term: Bass swap
slug: bass-swap
aliases: [EQ swap, low swap, bass handover, center bass, end bass swap]
group: Engineering and product
level: 1
one_liner: The moment in a blend where the outgoing track's bass is cut and the incoming track's bass comes in, on one downbeat.
why_it_matters_here: Two basslines at once sound muddy and can overload a club system, so where and how sharply the swap lands decides whether a blend sounds clean.
related: [isolator-eq, mix-region-and-mix-point, lanes-and-presets, stem-swap, beat-grid]
sources:
  - Spotify Support, Mixed playlists, https://support.spotify.com/us/article/mixed-playlists/
  - Wikipedia, Beatmatching, https://en.wikipedia.org/wiki/Beatmatching
status: draft
---

## In plain words

In a long blend you bring the new track in with its low EQ turned down, so only its highs and
mids join. Then, on a downbeat, you turn the old track's lows down and the new track's lows up at
the same time. That is the bass swap. Done on the bar line, the groove passes from one kick and
bassline to the other without a bar of mud or a bar of nothing.

Where it falls is a choice. A center swap lands at the mix point, in the middle of the blend, so
the new track owns the floor for the second half. An end swap holds the old bass until the blend
finishes, so the old track keeps the weight longer. Reviewers report Spotify's EQ lane offering
both, as Center bass and End bass swap.

## Picture

```text
  bars        1   2   3   4 | 5   6   7   8
                            |
  CENTER  A lows  ##########|..............
          B lows  ..........|##############
                            ^ swap at the mix point

  END     A lows  ##########################|
          B lows  ..........................|####
                                            ^ swap at the end

  never   A+B lows ######## both on  > mud, too much low end
```

## In Open DJ

Not yet as a transition. The crossfade curve menu lists bass swap as a disabled option
(`apps/webui/frontend/src/lib/components/rb/mixer/CrossfadeCurveSelect.svelte`,
`lib/rb/prefs.svelte.ts`); only the magic curve plays today. AutoPlay Next ducks the low EQ
before a hard cut. The AutoDJ research plans a `bass_swap_blend` style of 16 to 32 bars with the
swap at the phrase midpoint (`(private path)`).
