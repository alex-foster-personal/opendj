---
term: Mix region and mix point
slug: mix-region-and-mix-point
aliases: [transition region, mix window, cue-out and cue-in, mix-in point]
group: Engineering and product
level: 1
one_liner: The mix region is the stretch of bars where two tracks overlap; the mix point is the downbeat inside it where the handover happens.
why_it_matters_here: Picking the region and point is most of what makes an automatic transition sound chosen rather than random.
related: [lanes-and-presets, bass-swap, beat-grid, try-er]
sources:
  - Spotify Support, Mixed playlists, https://support.spotify.com/us/article/mixed-playlists/
  - Spotify Newsroom, Mix your favorite playlists, Tue 19 Aug 2025, https://newsroom.spotify.com/2025-08-19/mix-your-favorite-playlists-seamlessly-by-adding-your-own-transitions/
status: draft
---

## In plain words

When two tracks blend there is a window where both are playing. Spotify's Mix editor calls this
stretch the mix region and offers lengths of 2, 4 or 8 bars when it has found a beat grid. Inside
it sits the mix point, a downbeat where the incoming track takes over. In the editor you drag the
waveform to move the point, and the region moves with it. The editor's green vertical line is the
preview playhead, not the point: the point is the region's center, where every preset's steps land
and the stop button sits. Spotify does not document the shape, but
the editor's curves show the region centered on the point: the incoming track rises across the first half, the outgoing falls across
the second.

DJs say the same thing as: where you mix in, how long you blend, and the bar the new track
takes over.

## Picture

```text
  bars        1   2   3   4 | 5   6   7   8
                            |
  outgoing A  ##############|###########.....
  incoming B  .....#########|################
                            ^
                        mix point, a downbeat
  |<-------- mix region, 8 bars -------->|

  2 bars    |<-->|        quick handover
  4 bars    |<------>|
  8 bars    |<-------------->|  long blend
```

## In Open DJ

Not yet as a feature. AutoPlay on main does a hard cut with a low-EQ duck, not a chosen region.
The AutoDJ research (`(private path)`) plans
`get_mix_windows(track_id, role)`: ranked mix-out windows on outros or breakdowns and mix-in
windows on intros or builds, each with a start bar and a maximum length. A transition plan then
names a cue-out bar on A, a cue-in bar on B, a length in bars and a style. Open DJ calls these
windows and plan events rather than a region and a point.
