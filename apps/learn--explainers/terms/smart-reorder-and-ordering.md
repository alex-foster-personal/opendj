---
term: Smart reorder and playlist ordering
slug: smart-reorder-and-ordering
aliases: [Smart Reorder, harmonize, set ordering, sequencing, beam solver, PLAY IT]
group: Engineering and product
level: 2
one_liner: Rearranging a playlist so each next track is close in tempo and key to the one before, in one tap.
why_it_matters_here: Most bad transitions are bad pairs; a good order removes the clashes before any mixing starts.
related: [camelot-wheel, beat-grid, try-er, rl-environment-reward-policy]
sources:
  - Spotify Newsroom, Smart Reorder, Wed 25 Feb 2026, https://newsroom.spotify.com/2026-02-25/smart-reorder-playlist-mixing/
  - Bittner et al., Automatic Playlist Sequencing and Transitions, ISMIR 2017, https://archives.ismir.net/ismir2017/paper/000086.pdf
status: draft
---

## In plain words

Spotify's Smart Reorder, added to Mix on Wed 25 Feb 2026, takes a mixed playlist and reorders it
"with BPM and key", in Spotify's words. Each pair then sits closer together, so beatmatching
stretches less and the keys clash less. DJs do the same by hand when they build a set, often
with the Camelot wheel and an eye on energy.

The precise problem is ordering: find the sequence whose track-to-track costs add up smallest,
a cousin of the traveling salesman problem. Trying every order is impossible past a dozen
tracks, so solvers search cleverly. A beam search builds the set left to right and keeps only
the best few partial orders at each step.

## Picture

```text
  before   124 8A > 128 3B > 122 9A > 126 8B > 120 4A
                  x jump    x jump    x jump    x jump

  after    120 4A > 122 9A > 124 8A > 126 8B > 128 3B
                  ok        ok        ok        far key

  beam search, keep the best 3 at each step
  step 1   4A    8A    9A
  step 2   4A>9A    8A>8B    9A>8A          others dropped
  step 3   4A>9A>8A    9A>8A>8B    ...
```

## In Open DJ

The PLAY IT solver in `apps/dj_copilot/solver.py` is a deterministic greedy beam search. It
scores each next track on key 0.35, BPM 0.35, energy 0.25 and artist repeat 0.05, follows a set
energy goal, and honors pinned opener, peak and closer tracks. It is served at
`POST /api/v1/play-it/{playlist_id}/solve` (`apps/webui/server/routes/play_it.py`). Unlike
Smart Reorder it also weighs energy, and it reports which constraints it could not meet.
