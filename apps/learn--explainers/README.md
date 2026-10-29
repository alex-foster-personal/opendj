# learn--explainers: in-app explainer assets

Started Tue 27 Oct 2026 at the maintainer's request: every term a DJ or a reader of our docs might not know
gets one explainer here, written once and reused everywhere: the published research briefs (as a
glossary sidebar), the repo docs, and later the app's own documentation, tutorials and learning
side. The learning side is a product goal: Open DJ should be the best place to learn to DJ because
it is interactive, demonstrates on the user's own music (or a vetted free library), and lets a
learner follow a recorded set dial turn for fader slide and take over at any point.

## What lives here

- `terms/<slug>.md`: one term per file, YAML front matter plus three short sections (see
  `TEMPLATE.md`). The ASCII picture is mandatory where a picture helps and must render in a
  monospace block without color. An entry whose front matter names `animation:` carries a frame
  animation.
- `animations/<slug>.txt`: the frame animations, one file per animated term, frames split on
  `@@` lines; 31 terms have one today.
- `build_index.py`: validates every entry (front matter keys, group, level, one-liner length,
  picture width, dangling related slugs, the two banned dash characters) and writes `index.json`,
  which the sidebar injector and, later, the app load. Its `GROUPS` tuple is the one place the
  group list and its order live; the index carries it as `groups`.
- `inject_sidebar.py` and `sidebar/`: build the glossary sidebar into an HTML page (see below).
- `TEMPLATE.md`: the schema every entry follows. Keep entries short: a curious DJ reads one in
  under a minute.
- Later: `articles/` (longer pieces we cite) and `citations.md` (the sources we rely on, with
  their licenses).

## Rules

- Plain words first, the precise term second, the formula last and only when it earns its place.
- Every entry says why the term matters in Open DJ and points at the file where it shows up, or
  says "not yet" honestly.
- Sources are public references (standards, manuals, textbooks, vendor pages). Never paraphrase
  GPL source code here; facts about a product's behavior cite its manual.
- American spelling; dates carry the weekday; never the characters U+2013 or U+2014; no real home
  directories, mailboxes or private addresses.
- Level 1 is for any DJ, level 2 for a curious DJ who wants to know what the knob does inside,
  level 3 for an engineer. Most entries are level 2.

## Consumers today

- The transitions research brief (Tue 27 Oct 2026) renders these as its glossary sidebar.
- `docs/glossary.md` stays the terse project glossary; entries here are the long form.

## Sidebar for an HTML page

`inject_sidebar.py <index.json> <in.html> <out.html>` builds the glossary sidebar into any HTML
page that has exactly one `</main>`, an inline `<style>` block, and the color tokens its docstring
lists. The sidebar's own CSS, markup and script live beside it in `sidebar/` (`sidebar.css`,
`sidebar.html` with `__COUNT__` and `__PAYLOAD__` placeholders, `sidebar.js`); the script only
embeds the index and places the three pieces. To refresh a page that already carries a sidebar,
remove the old block first (everything from the `/* glossary sidebar` comment to the closing
`</style>`, and from the Terms button through the sidebar script after `</main>`), then inject
again. The transitions research brief (Tue 27 Oct 2026) is the first page that uses it.

## Left for a human (from the Tue 27 Oct 2026 check pass)
- About 14 entries still say "the transitions research", "the macro quality measure research" or
  "the playlist-ordering research" as a source of plans (auc, held-out-set, noise-band-and-bootstrap,
  mutation-test, bradley-terry, calibration-against-ratings, parity-capture, try-er, p95-and-percentiles,
  isolator-eq, full-kill-eq, shelf-eq, hamiltonian-path-and-tsp, beam-search). They name no private path but
  point at unpublished work; decide whether to attribute or reword.
- Several entries cite the tracked `(private path)` as "the AutoDJ research"
  (bass-swap, mix-region-and-mix-point, feedback-and-ping-pong, echo-vs-delay-vs-reverb, lanes-and-presets,
  stem-swap, try-er). It is tracked, but confirm it belongs in the public mirror.
- Source quality: Spotify, Bittner 2017, Mosaikbox and Mixxx citations were not re-verified online.
- Near-duplicates (not merged): lufs vs integrated-short-term-momentary vs k-weighting (one measurement
  family); equal-power-crossfade vs gain-ramp vs summed-loudness-bump; loudness-continuity vs
  trim-and-loudness-matching; beam-search vs hamiltonian-path-and-tsp vs smart-reorder-and-ordering;
  external-validation vs held-out-set vs calibration-against-ratings; stretch-engines vs time-stretch;
  key-clash vs relative-and-parallel-keys vs camelot-wheel; lanes-and-presets vs mix-region-and-mix-point.
