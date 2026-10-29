---
term: Parity capture
slug: parity-capture
aliases: [live versus render parity, render parity check, golden comparison, playback capture]
group: Evaluation and method
level: 2
one_liner: Record what the live engine actually played and compare it with the offline render of the same plan, within a stated tolerance.
why_it_matters_here: The scorer judges the offline render, so the score only describes what the crowd hears if live playback matches that render.
related: [offline-render, audio-clock-vs-page-tick, sample-accurate-scheduling, lufs, loudness-continuity]
sources:
  - Feathers, Working Effectively with Legacy Code, Prentice Hall, 2004 (characterization tests)
  - EBU Tech 3341, Loudness Metering, EBU, 2016 (loudness units used for the level tolerance)
  - Wikipedia, "Regression testing" (secondary pointer)
status: draft
---

## In plain words

To judge a transition quickly we render it offline: the computer works out the mixed audio
faster than real time, with no speakers involved, and the scorer measures that file. But the DJ
hears the live engine, which has its own clock, buffers and scheduling. If the two disagree, the
score is about a sound nobody heard. A parity capture closes that gap: play the same plan on the
same tracks through the live path, record what actually came out, line it up with the render,
and compare what matters, mainly when each beat lands and how loud it is. Pass means every
difference stays inside a stated tolerance; fail means the live path drifts, and that is a bug
in playback, not in the transition. Note the name clash: `apps/parity` is something else, it
scores our track analysis against rekordbox's.

## Picture

```text
 kick onsets, same plan, same two tracks

 offline render  #.........#.........#.........#.........#
 live capture    .#.........#.........#..........#..........#
 offset in ms     2         2         2          9          11
                  |--- inside 5 ms ---|   drift, past 5 ms, FAIL

 loudness        render -8.1 LU, live -8.3 LU, 0.2 LU apart, inside 0.5

 verdict  the level matches, the timing drifts after beat 3
          so the live clock is not following the render clock
```

## In Open DJ

Not yet built. The quality evaluation research (Tue 27 Oct 2026) lists the parity capture as
missing and recommends it as a standing gate, and the macro quality measure makes it a veto:
the live capture may not differ from the render by more than 5 ms or 0.5 LU on any fixture. The
render side exists in `apps/audio-engine/src/plan.rs`; the recording piece would most likely
build on the engine's capture code (`apps/audio-engine/src/capture.rs`), with the comparison
living next to the transition scorer.
