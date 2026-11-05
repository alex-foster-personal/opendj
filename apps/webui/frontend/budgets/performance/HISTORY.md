# `performance` budget history before charges

Write-once record, moved verbatim from the comment above this surface's `BUDGETS` entry in
`scripts/bundle-budget.mjs` on Tue 3 Nov 2026 (ADR-NEW-bundle-budget-charges). The last line
is the entry itself. Its limit, 324608, is this surface's base.json limit unchanged; its
recorded `measured` was 323481. New growth is a charge file, never an edit here.

```text
Merge note (PR #6163 x main 0ba0fa270, Thu 15 Oct 2026): #6163 had taken the
same +1 KiB (to 279,552) for the STRUCT-06 own-fast sections race and resync fixes
in anlz-cache and analysis-source. Both raises land on one ceiling; no second
raise. main + this PR measures 279,023 locally with vite build (529 under).
Wed 2 Sep 2026 18:40: +1 KiB for audio-output-liveness (P0: "no audio" must be an error
state; main had 24 bytes of headroom). Payback: PR #695 ships signalsmith-stretch once.
Thu 10 Sep 2026: +12 KiB for the isSuperseded() supersession-guard fix
(PR #1010, discussion_r3975326238). CI measured 216,952 on this diff.
Merge note: PR #1587 (main) independently measured 216,124 with its own
unrelated changes and a smaller ratchet (227,328); PR #1010's larger
ceiling is kept since it is the wider of the two and both diffs are now
present on this branch. Re-measure on the merged head before tightening.
Fri 11 Sep 2026: 223 -> 236 KiB, inherited trunk growth found while landing
#1555 (nav1-key-record). See the header comment above for the measurement.
Sat 26 Sep 2026 (PR #3837, Reloop Mixtour Pro I/O + MIDI + Play from USB):
NOT raised. The branch carried two +1 KiB raises that were never proposed
for main (Fri 25 Sep, IOPIN I/O panel MIDI status; Sat 26 Sep, Play from
USB stick-id routing); landing reverts them to this limit. The PR as written
measured 252,605 against it, +10,941 over, because the whole MIDI engine
(WebMIDI/native transport, device-map registry with the new Mixtour Pro
map, action glue, controller pad runtime, takeover policy) and the MIDI
drawer were in this route's static closure while being inert until
requestMidiAccess() resolves. Deferred instead: the engine behind one
dynamic import (lib/rb/midi/midi-engine.ts) awaited inside
requestMidiAccess() before initMidi() installs any port handler; midiState
split to a runtime-free leaf for the headphone cluster's status glyph; the
drawer and learn-log pop-out mounted only while open. Measured after:
239,199 (clean origin/main a3ca2f14a: 238,317), so the route pays +882 for
the PR's always-rendered chrome, and that weight moved to other-lazy.
RAISED Fri 2 Oct 2026 (+1 KiB, PR #4923, V1 UI polish): clean origin/main
5852c97c measures 239,840 locally. This PR adds +1,954 of user-visible
V1 work on the route (the 2-deck toggle, SET OUTPUTS and the I/O pins,
explainer dismiss, the 3Band waveform palette with its legacy option,
keyboard navigation in the track table, SVG MIDI marks), 241,794 merged,
130 bytes over the old limit; 894 bytes of headroom remain.
RAISED Fri 2 Oct 2026 (+2 KiB, PR #4014, issue #3986 library pins): the
library pane on /performance gains the configurable compatible filter
(Camelot steps, BPM window and direction), the purple pairing underline
(PairingIndex + the reference-master derivation), the BPM provenance hover
and the in-app confirm flow. BrowserConfirmDialog and the compatible-filter
range panel load with a dynamic import on first use. Clean origin/main
c8b8f5aeb measured 242,422 locally (266 bytes of headroom); main + this PR
measured 244,327, +1,905, leaving 409 bytes under the new limit.
RAISED Fri 2 Oct 2026 (+1 KiB, PR #4904, V1 copy and paste between
playlists): clean origin/main c8b8f5ae measures 242,439 locally (249
bytes of headroom). The library browser is part of /performance, and the
Cmd+A/C/X/V handlers add 1,264 bytes there. Loading the clipboard rules
lazily (./track-clipboard behind loadTrackClipboard) pays back 388 of
them; 243,315 merged, 627 bytes over the old limit, 397 bytes remain.
Merge note (PR #4014 x #4904): both diffs land on this head; #4014's
ceiling (244,736) is the larger of the two independent ratchets but does
not include #4904's clipboard handlers stacked on the #3986 work. Limit
raised to 245,760 (240 KiB) until the merged head is re-measured in CI.
RAISED Sat 3 Oct 2026 (+1 KiB, PR #4014, issue #3986 library pins; JIK's V1
budget decision, Sat 3 Oct 01:36Z, up to 4 KiB per V1 PR): re-merged with main
5504f60e7, /performance measured 245,836, 76 bytes over 245,760: main's own
growth since the last merge plus this PR's two-track pairing readiness check in
the top bar. 948 bytes remain. Payback: the same compatible-filter deferral as the library entry.
RAISED Fri 2 Oct 2026 (+1 KiB, PR #4906, STEM-44..48 / PERFMODE-18): the live stem
handoff and the graded decode hold are genuine route weight. Clean origin/main
c8b8f5aeb measured 242,439 locally; main + this PR 243,377, 689 bytes over.
Re-measured after #4904 landed on main (b6c894194) with its own +1 KiB raise:
both together measure 244,434, so this PR keeps its +1 KiB on top; 302 bytes remain.
RAISED Sat 3 Oct 2026 (+1 KiB, SET-11 set recording without ffmpeg, V1): REC now
lights only once audio is really written, and shows "Waiting for microphone
permission" while macOS's first-run prompt is up (Silver lost 42 s of a set to a
REC that looked live during it). recordRailState, its tooltips and the 1 s status
poll sit on the REC rail, which is first paint. Main measured 243,599 on Sat 3 Oct
(ci/2026-10-03-quality-ratchets.md); this branch 243,933, +334 of its own and 221
over the old limit. #4906 raises to the same 244,736 for its own +1,319: if it
lands first, the merged tree needs one more KiB. Payback, not yet measured: load
the REC tooltips with the lazy RecordInputPicker.
MERGED Fri 2 Oct 2026 (af--preview-live into main c8b8f5ae): main's #4923
raise is kept because its features are all in the merged route; the
Preview's #3837 work fit the old 241,664 by deferral (+882 measured). NOT
re-measured on the merged tree at resolution time.
MEASURED Fri 2 Oct 2026 on the merged tree (claude/port-preview-ui-to-main):
250,293, 7,605 over 242,688. Both trees were reviewed under their own
ceilings; the union carries main's V1 polish AND the Preview's I/O panel,
Mixtour pickup, phase lock and USB play on /performance. Reviewed raise to
the measured size ceiled to KiB (245 KiB), no extra headroom; flagged in
(private path). Payback candidates: the I/O panel body
and the USB browse store behind their first open.
MEASURED again Fri 2 Oct 2026 after main eede75a4 and the Preview's #5000
(enrich card collapses beside the help buttons) merged in: 251,283, 403
over 245 KiB. Raised to the measured size ceiled to KiB (246 KiB), no extra
headroom; the same performance-register row covers it.
RAISED Fri 2 Oct 2026: 237 -> 252 KiB for PR #5013 (V1 ports from
main-electron-rust). Beat Sync's continuous phase lock (#4602, #4653, #4733:
phase-lock-webaudio, phase-lock, context-time-wait, the preset runner split)
is real always-loaded deck weight on this route; on main-electron-rust the
same code shipped under a 249 KiB ceiling. Local build on this head measured
245,752 (CI agreed to the byte), 3,064 over. The ceiling follows the +5%
ceil-to-KiB rule on 245,752. Re-measured after merging main (#4904 copy
and paste, #4766): 246,970.
Merge with main after #4014/#4094/#4906/#5228: main's ceiling 260,096
(measured 247,657) is the larger of the two, so the merged tree keeps it.
RAISED Sat 3 Oct 2026 (+1 KiB, PR #4094, FB-20 comment hover counts; JIK's V1
budget decision, Sat 3 Oct 01:36Z, up to 4 KiB per V1 PR): the pin-summary
hover rides the /performance FeedbackWidget. Main 5504f60e7 + this PR
measured 243,886 locally, 174 bytes over 243,712 (CI measured 243,884); 850
bytes remain. Payback, not yet measured: load the summary popover lazily.
Merge note (PR #4014 x SET-11 x #4094 x #4906): main's merged ceiling is
260,096. The Preview port's reviewed ceiling was 251,904. This merge keeps
the larger of the two.
RAISED Mon 5 Oct 2026 (+1 KiB, PR #5441 CHROME-12/13, V1 budget rule of up to
4 KiB per V1 PR): the top-bar skin button cycles Default, Gothic and Light, and
set_skin carries the skin over IPC. CI measured 260,232 against 260,096 (+136).
RAISED Mon 5 Oct 2026 (+1 KiB, PR #5471 AGENT-18, V1 budget rule of up to 4 KiB per
V1 PR): one leader tab per engine. The Web Locks controller, the leased mirror
publisher and the follower banner are first-paint on /performance because the
election must run before the first mirror PUT. CI measured 261,862 against 261,120
(+742). Payback, not yet measured: load TabLeaderBanner only when a tab is a follower.
RAISED Mon 5 Oct 2026 (+1 KiB, PR #5482 AGENT-18 follow-up, V1 budget rule): follower
tabs are pure viewers (leader-only writers) and the playing tab claims the lease
(decideFollowerClaim). Local build measured 262,224 against 262,144 (+80). Payback
is the same as #5471's: load TabLeaderBanner only when a tab is a follower.
RAISED Mon 5 Oct 2026 (+2,229 B, PR #5474 RESCUE-07 reload-resume banner, CORE
decision Mon 5 Oct 23:25Z): CI measured 263,349 against 263,168 (+181); the raise is
that overage plus 2 KiB headroom. v1 launch, re-tighten post-v1.
RAISED Tue 6 Oct 2026 (+1 KiB, PR #5533 ENRICH-02, V1 budget rule of up to 4 KiB per
V1 PR): the enrich card names where BPM and key come from, the drain's real state and
the folders of files not on this Mac. The card is first paint on /performance. CI
measured 265,423 against 265,397 (+26). Payback, not yet measured: load the expanded
card body only when More is pressed.
RAISED Tue 6 Oct 2026 (+3,355 B, shared build-16 budget PR, CORE rule: one budget PR for the
build-16 set instead of per-PR raises). Clean origin/main ef76df4099 measured 266,105 locally
(316 B of headroom). Each PR merged alone onto that main: #5577 boot-walk settle +13
(estimate reserved +200 for its later head), #5565 lazy library-index assets +384, #5573 Beat Sync
Max enables sync +42, #5576 ENRICH-03 card R1-R4 +485, #5570 drain QoS 0 (backend only). All five
merged measured 267,017 (the shares add up to within 1 B). #5572 SET-12 is reserved at +512, not
yet measured. Projected tip 267,728: 1,307 over 266,421, plus 2 KiB headroom. Payback: issue #5522.
RAISED Tue 6 Oct 2026 (+2,178 B, shared build-17 / batch-2 budget PR, CORE rule: one raise for
the set, extends #5582). Clean origin/main 5cb903d82a measured 269,151 locally (625 B of headroom;
#5588, #5593 and #5600 are already in it). Each batch-2 PR merged alone onto that main: #5599 +589,
#5565 +353, #5607 +160, #5579 +83, #5615 +82, #5611 +1, #5605 -3, #5603 -4, #5602 -50, #5595 -731,
#5610 0 (server only); #5587 conflicts alone, reserved +275 (its author's +358 for #5579/#5587 less
#5579's measured +83). Net +755, projected 269,906: 130 over 269,776, plus 2 KiB. Without #5595's
-731 the tip would be 270,637, still under the new limit. Payback: issue #5522.
RAISED Thu 1 Oct 2026: 236 -> 249 KiB for PR #4764 (Crate Desk, LIBWS-01).
Library mode now mounts the same BrowserPanel Gig does, behind a dynamic
import from "/" so the library first paint stays under its unchanged ceiling
(local: 256,543, base 256,695). Sharing the browser with a lazy importer
makes Rollup split what was one 215,093 chunk on the base into two
(110,383 + 106,988 = 217,371): about 2.3 KB of lost cross-chunk compression,
plus the workspace guards in BrowserPanel, TrackTable and TrackContextMenu.
Local, both sides on a clean `git worktree add` build: base 113f4b99
measured 239,584 over 18 files, this head 242,549 over 21, +2,965. No eager
import can be demoted to recover it: the browser IS this route's UI. The
ceiling follows the +5% ceil-to-KiB rule on 242,549.
Ported to main by the Crate Desk port (#4764): main's larger ceiling above is kept;
see the next RAISED/MEASURED note for the measurement on the ported tree.
RAISED Tue 6 Oct 2026 (+3,502 B, Crate Desk port of #4764 to main, LIBWS-01; within the
V1 rule of up to 4 KiB per V1 PR; draft, held for JIK's product call). Local, one build
each, clean `git worktree add`: origin/main 4d00338f6 measured 269,714 over 22 files; this
branch 272,784 over 25, +3,070 (830 over the old limit). Same mechanism the source
measured (+2,965): "/" now lazy-imports the shared BrowserPanel, so Rollup splits the
browser out of this route's chunk and the split loses cross-chunk compression, plus the
Library-mode guards in BrowserPanel, TrackTable and TrackContextMenu. The raise is the
measured size ceiled to KiB (273,408) plus 2 KiB headroom. Payback: none open; the
browser is this route's UI and stays eager.
RAISED Fri 16 Oct 2026 (+3,072 B, part C of #6211, REACT-01 `ui.react.deck`; within the V1
rule of up to 4 KiB per V1 PR). Local, one clean build each: part A 479c89602 (main
a7a1ef3fb plus the tooling) measured 274,759 over 24 files; this head 275,958 over 24, +1,199
(502 over the old limit). The weight is ReactIsland, the flag read in Deck and HotCueBank, and
the seven twin loader stubs; react, react-dom and the twins themselves stay lazy in
other-lazy, so a build with the flag off fetches none of them. The raise is the measured size
ceiled to KiB (276,480) plus 2 KiB headroom. Payback: when the v2 shell is React, the islands
and the Svelte originals they wrap leave this route together.
RAISED Thu 22 Oct 2026 (+3 KiB, PR #6235, GRIDEDIT-6235..6235d + DECKCTL-6235..6235d: grid adjust and
shift buttons and their request queue, AU, MA tempo editor with TAP, Q phase, INT loop source
menu; JIK's V1 rule, up to 4 KiB per V1 PR). Local vite build: main c454c7b27 276,520; main +
this PR 280,957 (+4,437, 2,429 over), of which the grid-edit request chunk moved off "/" is
about 1.5 KiB. Payback: none open; these are six live deck controls replacing inert ones.
Merge note (PR #6235 x main 4c130633d, Tue 27 Oct 2026): with main's own growth this PR
measured 283,545 (main alone 278,339, +5,206), 1,945 over. Paid back inside the same raise:
the grid-edit queue and its re-read now load on the first event or press, and the tempo
editor (MA, double-click on BPM) on first open. Local vite build: 280,203 (+1,864).
RAISED Thu 22 Oct 2026 (+1 KiB, PR #6819, re-land of #6195 stem separation UI, V1 rule of
up to 4 KiB per V1 PR): the /performance decks ship the "Separate this track" button and
the deck's separation view. Local, one clean build each: main b447c807c measured 276,554
over 25 files; this head 278,643 over 27, +2,089 (115 over the old limit). The separation
store itself stays lazy. Payback, not yet measured: load the separation view on first use.
RAISED AGAIN Tue 27 Oct 2026 (+2 KiB more, same PR, merge skew; +3 KiB over main's limit in
all, within the V1 rule): CI's build of this head merged with main 4c130633d1 measured
280,526 over 27 files, 974 over the earlier raise, because main itself grew the route since
b447c807c while this PR's own share stayed +2,089. The limit is that measurement ceiled to
KiB (281,600). Same payback as above.
RAISED Tue 27 Oct 2026 (+3,072 B, top bar re-land #6824 of #6201, TOPVIEW-01..04; within
the V1 rule of up to 4 KiB per V1 PR). On Thu 22 Oct it fit with no raise: its earlier
+3 KiB was paid back by loading the two phase views (PerfViewArea.svelte) on first use,
and main b447c807ce 276,572 plus this PR measured 278,123. Main has since grown to within
138 bytes of the limit. Locally, one clean build each: main 4c130633d1 278,390; main +
this PR 280,095 (+1,705). The weight is the top bar's view switch, Pads toggle and two-deck
auto-play control, which render on this route. The raise is the measured size ceiled to
KiB (280,576) plus 1 KiB of jitter. Payback: none open; the controls are this route's UI.
Merged onto main e5fb30dcfd Wed 28 Oct 2026 (#6819 took main's ceiling to 281,600):
locally, main 280,509; main + this PR 282,162 (+1,653). RAISED to the measured size ceiled
to KiB (282,624) plus 1 KiB of jitter, +2 KiB over main's ceiling; same payback.
RAISED Thu 15 Oct 2026 (+2,048 B, USB export re-land #6826 of #6191, USBEXP-01..04; within
the V1 rule of up to 4 KiB per V1 PR). Local, one build each: main merge-base measured
273,395; this branch 275,553 (+2,158, 97 over the old limit). Most of it is the
`usb-export.svelte` store joining the shared chunk first paint loads (about 2 KB); the
lazily loaded stick menu adds about 90 B of re-exports. The extra KiB of headroom covers
the 2-3 KB gap other PRs have measured between CI and local builds. Payback: lazy-load
the usb-export store behind the stick tree, logged in (private path).
Same day, same PR: the single in-flight operation guard in the usb-export store
(close and re-entry refused while a plan, write or eject runs) moved it to 275,859
(+306 B, local, one build); the limit is unchanged.
RAISED Fri 16 Oct 2026 (+1,024 B, same PR, now +3 KiB of its 4 KiB V1 allowance): CI
measured 277,936 on c76d0a3317, 432 over 277,504, after the write-warning, quit-close and
vanished-stick fixes in the usb-export store. Same payback as above.
Merged onto main b447c807ce Thu 22 Oct 2026: main took its own +3 KiB (#6211 part C,
above) to the same 278,528 ceiling, so this PR's raise now lands on top of it. Locally,
one clean build each: main 276,572; main + this PR 280,023 (+3,451). This PR's raise is
+2 KiB over main's ceiling (280,576), within its 4 KiB V1 allowance; same payback.
RAISED again Thu 22 Oct 2026 (+1,024 B, same PR, now +3 KiB over main's ceiling, within its
4 KiB V1 allowance): the review fixes since (eject race, plan envelope check, malformed
volume listing refused, USB bus filter, literal verified check) measured 280,596 locally,
20 over 280,576. Same payback as above.
Merged onto main e5fb30dcfd Wed 28 Oct 2026 (#6819 took main's ceiling to 281,600): this
PR's raise now lands on top of it. Local, one clean build each: main 280,509; main + this
PR 285,249 (+4,740). RAISED to +4 KiB over main's ceiling (285,696), the whole V1
allowance, with 447 B headroom; same payback as above.
Merged onto main again Wed 28 Oct 2026, after #6824 took main's ceiling to 283,648 (above):
locally, one clean build each, main + #6824 282,162; main + this PR 287,067 (+4,905, the
stick-action UUID gate included). RAISED to +4 KiB over main's ceiling (287,744), the whole
V1 allowance, with 677 B headroom; same payback as above.
RAISED Wed 28 Oct 2026 (trunk repair #7658): main 275b88e7f8 is over this budget after
#7594 (autodj defects + agent DJ control) and #6565 (MIK energy backfill) landed through
selected-test bundle merges (#7598) that did not run the budget gate. One clean nucbox
build each: main 906dc7be74 287,062; main 275b88e7f8 287,946 (+884).
Measured ceiled to KiB plus 1 KiB of jitter. Payback: owed by #7594 and #6565.
RAISED Wed 28 Oct 2026 (+2 KiB, PR #6574, KEYSHIFT-01..05, merged onto main 27263feb59):
main's limit plus this PR's own delta, ceiled to KiB; the delta (+1,830) is from one clean
nucbox build each of main 906dc7be74 with and without this PR. Payback: none planned.
measured below is this merged revision's own nucbox build: 289,877.
RAISED Wed 28 Oct 2026 (+1 KiB, PR #7600, LIBUX-53 collapse rule, re-merged onto main after #6574):
this PR's measured delta on top of main's limit (#6574's), ceiled to KiB. One clean nucbox build
each against main 906dc7be74 measured +343. Payback: none planned.
RAISED Wed 28 Oct 2026 (+2 KiB, PR #7601, LIBUX-54 column chooser, re-merged onto main after #7600):
this PR's measured delta on top of main's limit (after #7600), ceiled to KiB. One clean nucbox build
each against main 906dc7be74 measured +1,027. Payback: none planned.
RAISED Wed 28 Oct 2026 (+2 KiB, PR #7574, QLTSCAN-02 audio quality grade, re-merged onto main after #7601):
main's limit plus this PR's own delta, ceiled to KiB; the delta (+1,143) is from one clean
nucbox build each of main 906dc7be74 with and without this PR. Payback: none planned.
RAISED Wed 28 Oct 2026 (+11 KiB, PR #6829, re-land of #6205, re-measured after merging
main b26ee5252c, #7700; see the LOWERED note on library): settings modules /performance
shares with the lazy overlay are charged here, plus the mixer port's own controls. Local,
one clean vite build each: main b26ee5252c library 290,576, performance 287,936, other-lazy 375,376 (953,888); main + this PR
274,187, 299,765, 394,367 (968,319), net +14,431 B across the three.
Over the 4 KiB V1 rule net of the move: flagged for the maintainer. Measured ceiled to KiB (300,032)
plus 1 KiB of jitter. Payback: none open.
Thu 29 Oct 2026 (re-measured after merging main 3dcda3b028, which now carries #6223's code): one clean vite build each, main 294,894 / 292,373 / 392,191 and main + this PR 277,411 / 304,114 / 412,036, deltas -17,483 / +11,741 / +19,845 B, ceiled to -17 / +12 / +20 KiB on main's 296,960 / 294,912 / 393,216, so 279,552 / 307,200 / 413,696 (+15 KiB net). The earlier carry measured before #6223 landed while its limit already counted #6223.
RAISED Fri 30 Oct 2026 (+2 KiB, trunk repair after #7957 and #6534; see the same note on
library): one clean vite build each, main 507102f2b9 306,705; main 8849e7a94e 307,648.
Measured ceiled to KiB (308,224) plus 1 KiB of jitter. Payback: owed by #7957 and #6534.
(Ported into PR #6189 from #8069; PR #6189 adds +20 B, inside this limit.)
Merge note (PR #6235 x main 131f65be61, Fri 30 Oct 2026): main's growth since 4c130633d (#6829's
lazy-settings move among it) re-bases the #6235 raises above onto main's ceiling. Local, one
clean vite build each: main 131f65be61 306,066; main + this PR 307,603 (+1,537).
RAISED +2 KiB over main's ceiling: the measured size ceiled to KiB (308,224) plus 1 KiB of
jitter, within the V1 rule of up to 4 KiB per V1 PR. Payback: none open; these are live deck
controls.
Merged onto main 8849e7a94e Fri 30 Oct 2026: main alone measures 307,648 locally, 448 B over its own
307,200 ceiling; main + this PR 309,407 (+1,759). RAISED to the measured size ceiled to KiB (310,272)
plus 1 KiB of jitter; 1 KiB of the +4 KiB over main's ceiling is main's own overrun. Payback: none open.
Merged onto main 2cd9af583b Sat 31 Oct 2026, after #8069's repair took this ceiling to 309,248: main measures
308,633 locally; main + this PR 310,340 (+1,707). RAISED to the measured size ceiled to KiB (311,296)
plus 1 KiB of jitter, +3 KiB over main's ceiling, within the V1 rule. Payback: none open.
RAISED Sat 31 Oct 2026 (PR #6835, re-land of #6123/#6196, merged onto main 29a59c570c): one clean local vite build
each, main 309,179 and main + this PR 311,628 (+2,449, this PR); the merged measurement ceiled to KiB => 312,320.
Merged onto main fd170f45c Sat 31 Oct 2026, after #6835 measured 311,628 against this same 312,320: one clean
local vite build each, main 312,098; main + this PR 313,726 (+1,628). RAISED to the measured size ceiled to
KiB (314,368) plus 1 KiB of jitter, +3 KiB over main's ceiling, within the V1 rule. Payback: none open.
Merged onto main d20ef4367 Sat 31 Oct 2026: main 312,067; main + this PR 313,717 (+1,650). Ceiled to KiB
plus 1 KiB the limit stays 315,392, +3 KiB over main's ceiling, within the V1 rule.
Re-land of #6221 (the I18 remote speaker), merged onto main cbd06578dd Sat 31 Oct 2026: one clean local
vite build each, main 314,035 and main + this PR 314,401 (+366); inside the limit above, so no raise.
RAISED Mon 2 Nov 2026 (+1 KiB, headroom repair, no feature): main sat 843 B under this limit, so unrelated
PRs tipped it at random. CI frontend job 125490304350 (main 8f3c5ac827) measured 314,475; one clean nucbox vite
build each, 8f3c5ac827 314,422 and main 9a89085862 314,496 (+74), so main on CI is 314,549. Measured ceiled to KiB
(315,392) plus 1 KiB of jitter => 316,416. Payback: none planned.
RAISED Mon 2 Nov 2026 (+1 KiB, PR #7896, wave plan curves, WAVEPLAN-7896): one clean local vite build each after
merging main 13515f0285: main 314,115 and main + this PR 315,311 (+1,196). On CI, main 1ac118b2f7 measured 314,475
(frontend job 125573392771), so main + this PR on CI is about 315,671, 745 B under the repair above. Ceiled to KiB
(316,416) plus 1 KiB of jitter => 317,440.
Mon 2 Nov 2026 (PR #7896, wave plan curves), merged onto main bb5d35d6a2: one clean local vite build measured
316,969, 471 B under the limit above, and CI has measured this surface about 450 B heavier than local builds.
The measured size ceiled to KiB (317,440) plus 1 KiB of jitter => 318,464.
RAISED Mon 2 Nov 2026 (+3 KiB, deck preview port, PR #7691, the DECKGAP-7691 family, merged main b1cdc8056c): one
clean vite build each, main 314,471 and main + this PR 316,584 (+2,113), ceiled to +3 KiB on main's 316,416, so
319,488. Payback: the reset panel can load on first open, as QuickDrawMenuLoader does.
Mon 2 Nov 2026 (PR #7896 merged onto main with #7691): one clean local vite build measured 319,049, and CI runs this
surface about 450 B heavier than local. The measured size ceiled to KiB (319,488) plus 1 KiB of jitter => 320,512.
RAISED Tue 3 Nov 2026 (fold-notice follow-up #7662, merged main 2dd433890d): CI job 125667086112 (head 61631aaf93)
measured 320,883. Main's CI job 125662168293 (main 1da0577f16) measured 320,885, so this PR is -2 B. Measured ceiled
to KiB (321,536) plus 1 KiB of jitter => 322,560. Payback: none planned.
RAISED Tue 3 Nov 2026 (PR #6802 wave zoom merged onto main 1da0577f16 with the fold-notice follow-up): the merged tree
measures 323,481 here, 921 B over 322,560. Ceiled to KiB (323,584) plus 1 KiB of jitter => 324,608. Payback: none
planned; the zoom and the plan overlay are the features on /performance.
RAISED Mon 2 Nov 2026 (+3 KiB, wave-mixer, PR #7692, merged main 7837e09617): one clean vite
build each, main itself measures 323,484; main + this PR 325,991 (+2,507). Carried onto main's 324,608, so
327,680. Payback: none planned. Charge file budgets/performance/charges/7692.json.
RAISED Tue 3 Nov 2026 (PR #7901 version stacks and works, merged onto main 80d4c76d90 with the v26 foundation): this PR
measured +7,872 B on one clean local vite build each against main ab7e1a6150. Added to main's 323,481: 331,353.
Ceiled to KiB plus 1 KiB of jitter => 332,800. Merge note (main wave-mixer #7692 x #7901): main's released raise
lands first; this ceiling stacks the version-stack feature on that trunk. Charge file budgets/performance/charges/7901.json.
Payback owed (over the 4 KiB V1 allowance): load WorksModals and VersionStackPanel on first use.
{ name: 'performance', limit: 324608, measured: 323481, note: '/performance and children' }
```

REBASELINED Thu 5 Nov 2026 (preventive rebaseline, surface within 3% of ceiling, Thu 5 Nov 2026; main 86084188b4, #8869, run 42023794899, frontend job 127238947586): CI measured 399,251 B gzip, 98.7% of base 361,472 + charges 43,008 (7898, 7901, 7909, 8626, 8728, 8801, 8869) = 404,480, about 5.2 KB left. Measured ceiled to KiB (399,360) plus 1 KiB => 400,384 by the README rule, superseded by measured plus 2% (399,251 x 1.02 = 407,236 => 407,552); the seven charges are folded into the base and deleted. Payback: none planned, the payback notes in the folded charge files still stand. Headroom note: the README's "next KiB plus 1 KiB" rule leaves headroom below build noise on large surfaces (local other-lazy read 486,102 against CI 485,775; library tipped on 15 B of noise on Thu 5 Nov 2026), so this preventive rebaseline uses CI measured plus 2%, rounded up to the next KiB: 399,251 x 1.02 = 407,236 => 407,552.
