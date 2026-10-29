---
term: Try-er
slug: try-er
aliases: [transition audition, candidate picker, A/B transition chooser]
group: Engineering and product
level: 2
one_liner: Render several candidate transitions for one pair, show their scores, let a person pick one, and save the pick so the chooser learns.
why_it_matters_here: It turns transition quality from a guess into something measured and taught by a real DJ's ear, one pick at a time.
related: [offline-render, mix-region-and-mix-point, lanes-and-presets, rl-environment-reward-policy, parity-capture]
sources:
  - Bittner et al., Automatic Playlist Sequencing and Transitions, ISMIR 2017, https://archives.ismir.net/ismir2017/paper/000086.pdf
  - Bradley and Terry, Rank Analysis of Incomplete Block Designs, Biometrika, 1952
status: draft
---

## In plain words

For one pair of tracks there are many ways to mix: cut, filter sweep, bass swap blend, longer or
shorter, starting on this phrase or that one. A try-er renders, say, six of them offline in a
second or two, scores each on things that can be measured (beats lined up, vocals meeting,
loudness dip, key clash), and lets you audition them from the same bar so switching is seamless.
You pick one. The pick is written back as the transition for that pair, and also kept as a
lesson: this one beat those ones.

Over many picks the chooser's weights move toward your taste. The precise form is a pairwise
preference model fitted to choices, with hard vetoes kept as gates.

## Picture

```text
  pair A > B
     |
     v
  candidates   A cut 1 bar      B filter 8      C bass swap 16
     |         D bass swap 32   E echo 2        F long blend 32
     v
  offline render each + score
     |         A 41   B 63   C 78 *  D 74   E 55   F veto vocals
     v
  audition from the same bar, A/B, pick C
     |
     +--> write C back as this pair's transition
     +--> log C beat A B D E  > chooser weights shift
```

## In Open DJ

Not yet built. It was designed in the transitions research of Tue 27 Oct 2026 as the critic in
the planner, executor and critic loop of `(private path)`.
It would sit on the offline renderer (`odj-audio render --plan --decks-out`,
`apps/audio-engine/src/offline.rs`) and a transition scorer, with live play still going through
the dispatcher. The first version is planned as human-picked; auto-pick comes after the scores
agree with the picks.
