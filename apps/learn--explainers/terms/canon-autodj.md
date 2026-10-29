---
term: Canon (the AutoDJ design doc)
slug: canon-autodj
aliases: [canon, the canon, canon doc, canon ladder, rung, rungs, AutoPlay to AutoDJ, TransitionPlan]
group: Engineering and product
level: 1
one_liner: The locked AutoDJ design doc: a six-rung ladder from AutoPlay to AutoDJ, the plan-executor-critic split, and the TransitionPlan vocabulary.
why_it_matters_here: PR #5822 builds the canon's first slice and the brief's words (styles, mix window, trim_db) come from it, so "the canon's X" means "what that doc specifies".
related: [lanes-and-presets, mix-region-and-mix-point, trim-and-loudness-matching, offline-render, ipc-dispatcher, try-er]
sources:
  - (private path) on main, status CANON since Tue 6 Oct 2026
  - PR #5822 (apps/autodj, the first slice, not on main as of Tue 27 Oct 2026)
  - docs/glossary.md, section Transitions and mixing (the vocabulary), branch af--docs-standard-set
status: draft
---

## In plain words

"The canon" is one document, `(private path)`, which the
product owner locked in as canon ("as canon / law") on Tue 6 Oct 2026. Anything about automatic
transitions follows it, and it changes only by a PR that edits the file and quotes his say-so.

It says three things. First, a good transition is a plan, not a fade: five decisions per pair
(where to leave the outgoing track, where to enter the incoming one, how many bars overlap, what
moves during the overlap, what happens to tempo). Second, a ladder of six rungs from AutoPlay to
AutoDJ: rung 0 starts the next track; 1 is a timed crossfade; 2 a structure-aware beatmatched
blend with gain matching; 3 a styled transition chosen per pair (bass swap, filter, echo out,
riser, stem swap, cut); 4 re-arranges the tracks (loops, extended parts, big tempo gaps); 5 is
intent-driven, a prompt drives selection and every transition and the set adapts live. Spotify
sits on 2 and 3; Open DJ's AutoPlay is rung 1. Third, the split: an LLM plans, a deterministic
executor performs the plan sample-accurately, and an offline render scores it before it goes
live. The plan is a `TransitionPlan` in absolute bars with a named style, parameters, and a
`trim_db` per deck, the gain offset in dB that matches the incoming track's loudness to the
outgoing one. "The canon's trim_db" in the brief means that field.

## Picture

```text
 rung 5  intent-driven AutoDJ prompt -> set and transitions
 rung 4  re-arranging tracks  loops, long parts, tempo gaps       Apple
 rung 3  styled per pair      bass swap, filter, echo, riser    Spotify
 rung 2  beatmatched blend    structure cues, gain match        Spotify
 rung 1  timed crossfade      fixed seconds, volume only     <- Open DJ
 rung 0  next track           no overlap                          today
 ----------------------------------------------------------------------
 plan (LLM) -> executor (sample-accurate) -> critic (offline render)
 TransitionPlan: out/in windows in bars, style, params, trim_db per deck
```

## In Open DJ

The document is on main at `(private path)`. Rung 1 lives in
`apps/webui/frontend/src/lib/rb/auto-play.ts` (pick by key and BPM, beat-synced start about 16 s
before the master ends, no fader moves). The first slice above it, styles, score_pair, compile and
the page executor, is PR #5822 under `apps/autodj`, not yet on main; the try-er build on top of it
started Tue 27 Oct 2026.
