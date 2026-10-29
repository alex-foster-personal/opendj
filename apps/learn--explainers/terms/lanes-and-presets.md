---
term: Lanes and presets
slug: lanes-and-presets
aliases: [automation lanes, transition presets, transition styles, curves]
group: Engineering and product
level: 1
one_liner: A lane is one control drawn as a curve across the mix; a preset is a saved setting for every lane at once.
why_it_matters_here: Lanes make a transition editable and visible; presets and Open DJ's styles make it one tap, and are the menu an automatic mixer chooses from.
related: [mix-region-and-mix-point, bass-swap, stem-swap, gain-ramp]
sources:
  - Spotify Support, Mixed playlists, https://support.spotify.com/us/article/mixed-playlists/
  - Spotify Newsroom, Mix your favorite playlists, Tue 19 Aug 2025, https://newsroom.spotify.com/2025-08-19/mix-your-favorite-playlists-seamlessly-by-adding-your-own-transitions/
status: draft
---

## In plain words

Spotify's Mix editor shows a transition as five lanes under the two waveforms: Volume, EQ,
Filter, Effects and Looping. Each lane is a curve, or a choice of curve, for one thing that moves
during the blend, the way automation lanes work in a music DAW. A preset fills in all the lanes at
once. The picker, as seen live on Tue 27 Oct 2026, offers Fade, Rise, Blend, Wave, Melt, Slam,
Dissolve, Surge, Afterglow and No transition, plus Auto and Custom; Spotify's own pages name only
the first three. Picking a preset fills every lane (none of the nine captured presets uses the
Effects or Looping lane), while Custom lets you pick per lane.

A DJ does all five by hand at once: faders, EQ knobs, a filter, an effect, a loop. Lanes simply
write that down.

## Picture

```text
  bars       1   2   3   4 | 5   6   7   8
  VOLUME   B .....#########|################
           A ##############|###########.....
  EQ       A lows on       | lows cut        swap on the downbeat
  FILTER   A ..............|....=====#######  high-pass rising
  EFFECTS  A dry           |       ###|      echo, cut at end
  LOOPING  A 2 bar loop repeating to the end......
                           ^ mix point

  preset Fade   =  fixed setting on every lane
  Open DJ style =  bass swap blend, 16 bars, swap at the midpoint
```

## In Open DJ

Open DJ calls a preset a style. The AutoDJ research (`(private path)`)
plans `list_transition_styles()` with cut, filter_sweep, bass_swap_blend, long_blend, echo_out,
stem_swap, loop_extend and drop_swap, each with parameter ranges. A style compiles to timed ramps
on the same controls the dispatcher drives, which play the part of lanes. No lane editor exists
on main yet. Spotify documents only the lane names; the per-preset lane values above were read off
the live editor one preset at a time on Tue 27 Oct 2026, and the reviewers' lists add a few more
option names that were not seen.
